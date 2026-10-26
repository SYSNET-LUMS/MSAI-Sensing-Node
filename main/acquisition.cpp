#include "node.h"
#include "resampler.h"
#include "calibration_config.h"
#include "deadline_recovery.h"
#include "driver/spi_master.h"
#include "driver/gpio.h"
#include "esp_timer.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "nvs.h"
#include <string.h>

namespace {
struct Input {uint32_t epoch;uint64_t stamp;int32_t s[64][8];};
QueueHandle_t inputs;spi_device_handle_t adc;TaskHandle_t adc_task;
volatile uint32_t edges=0;
std::atomic<bool> adc_idle{true},dsp_idle{true};
uint8_t active=0;float voltage[3];bool voltage_cal[3]={};
void IRAM_ATTR ready(void*) {++edges;BaseType_t wake=pdFALSE;vTaskNotifyGiveFromISR(adc_task,&wake);if(wake)portYIELD_FROM_ISR();}
uint16_t crc_table[256];
uint16_t IRAM_ATTR crc16(const uint8_t *p,size_t n) {uint16_t c=0xffff;while(n--)c=uint16_t((c<<8)^crc_table[((c>>8)^*p++)&255]);return c;}
uint8_t rx[30];
esp_err_t spi_result=ESP_OK;uint16_t crc_expected=0,crc_received=0;
bool IRAM_ATTR frame(uint16_t command=0,uint16_t value=0) {
  uint8_t tx[30]={};tx[0]=command>>8;tx[1]=command;tx[3]=value>>8;tx[4]=value;
  spi_transaction_t t{};t.length=240;t.tx_buffer=tx;t.rx_buffer=rx;
  // Match the proven board driver: assert CS before starting the SPI engine.
  gpio_set_level(GPIO_NUM_10,0);
  spi_result=spi_device_polling_transmit(adc,&t);
  gpio_set_level(GPIO_NUM_10,1);
  crc_expected=crc16(rx,27);crc_received=uint16_t(rx[27])<<8|rx[28];
  return spi_result==ESP_OK&&crc_expected==crc_received;
}
uint16_t IRAM_ATTR response(){return uint16_t(rx[0])<<8|rx[1];}
bool adc_error(const char *stage,uint8_t reg,uint16_t expected=0){
  ESP_LOGE("adc","%s reg=0x%02x expected=0x%04x response=0x%04x spi=%s crc=0x%04x/0x%04x drdy=%d",stage,reg,expected,response(),esp_err_to_name(spi_result),crc_expected,crc_received,gpio_get_level(GPIO_NUM_9));
  ESP_LOG_BUFFER_HEX_LEVEL("adc",rx,sizeof(rx),ESP_LOG_ERROR);return false;
}
bool read_reg(uint8_t r,uint16_t &v){if(!frame(0xa000|(uint16_t(r)<<7))||!frame())return adc_error("read",r);v=response();return true;}
bool write_reg(uint8_t r,uint16_t v){
  uint16_t actual=0,ack=0x4000|(uint16_t(r)<<7);
  if(!frame(0x6000|(uint16_t(r)<<7),v)||!frame())return adc_error("write frame",r,v);
  if(response()!=ack)return adc_error("write acknowledgement",r,ack);
  if(!read_reg(r,actual))return false;
  return actual==v||adc_error("readback",r,v);
}
bool init_adc() {
  gpio_set_level(GPIO_NUM_14,0);vTaskDelay(pdMS_TO_TICKS(1));gpio_set_level(GPIO_NUM_14,1);vTaskDelay(pdMS_TO_TICKS(5));
  if(!frame())return adc_error("reset frame",0);
  uint16_t id=0;uint16_t osr=config.rate==10000?1:config.rate==2000?4:5;
  if(!read_reg(0,id))return false;
  if((id&0xff00)!=0x2800)return adc_error("device ID",0,0x2800);
  bool ok=write_reg(2,0x0111)&&write_reg(3,0xff82|(osr<<2))&&write_reg(4,0)&&write_reg(5,0);
  for(int ch=0;ch<8&&ok;++ch)ok=write_reg(9+5*ch,0);
  if(ok)ESP_LOGI("adc","Ready: id=0x%04x clock=0x%04x gain=1 channels=8",id,0xff82|(osr<<2));
  return ok;
}
bool IRAM_ATTR hardware_ok(){return !reconfiguring&&selectors()==active&&active!=3&&gpio_get_level(GPIO_NUM_2)&&feedback()==((active&1)?15:0);}
void IRAM_ATTR acquire(void*) {
  DeadlineRecovery recovery;
  Input block{};unsigned count=0,settle=32;uint32_t last=0,e=0,rate_edges=0;int64_t last_us=0,rate_us=0;bool bus_owned=false;
  for(;;) {
    if(!acquiring){if(bus_owned){spi_device_release_bus(adc);bus_owned=false;}adc_idle=true;vTaskDelay(1);continue;}
    adc_idle=false;if(!acquiring)continue;
    if(!bus_owned){ESP_ERROR_CHECK(spi_device_acquire_bus(adc,portMAX_DELAY));bus_owned=true;}
    if(e!=epoch){e=epoch;count=0;settle=32;frame();frame();last=rate_edges=edges;last_us=rate_us=esp_timer_get_time();}
    ulTaskNotifyTake(pdTRUE,pdMS_TO_TICKS(3));if(!acquiring)continue;
    uint32_t fresh=edges;int64_t now=esp_timer_get_time();
    if(fresh==last){if(now-last_us>10000)fail(8);continue;}
    uint32_t delta=fresh-last;int64_t interval=now-last_us;last=fresh;last_us=now;
    if(!hardware_ok()){if(acquiring&&!transport_paused)fail(3);continue;}
    if(delta>1&&!settle){
      missed_adc+=delta-1;
      const uint32_t native_rate=config.rate==10000?16000:config.rate;
      if(recovery.allow(delta,interval,now,native_rate)){
        ++adc_recoveries;
        pause_transport("ADC timing recovery");
        ESP_LOGW("adc","Timing gap: edges=%lu interval=%lld us; discarding partial data, restarting in a new epoch",(unsigned long)delta,(long long)interval);
      }else{
        fail(5);ESP_LOGE("adc","Repeated or excessive missed deadline: edges=%lu interval=%lld us",(unsigned long)delta,(long long)interval);
      }
      continue;
    }
    if(!frame()){++adc_crc_errors;fail(6);continue;}
    if(!acquiring)continue;
    if((response()&0x7f00)!=0x0100||(response()&255)!=255){fail(7);ESP_LOGE("adc","Unexpected status=0x%04x interval=%lld us",response(),(long long)interval);continue;}
    if(settle){--settle;continue;}
    if(now-rate_us>=250000) {
      double hz=(fresh-rate_edges)*1000000.0/(now-rate_us);double target=config.rate==10000?16000:config.rate;
      if(hz<target*.97||hz>target*1.03){fail(10);continue;}rate_us=now;rate_edges=fresh;
    }
    if(!count){block.epoch=e;block.stamp=now;}
    for(int c=0;c<8;++c){const uint8_t*p=rx+3+c*3;uint32_t raw=uint32_t(p[0])<<16|uint32_t(p[1])<<8|p[2];block.s[count][c]=(raw&0x800000)?int32_t(raw|0xff000000):int32_t(raw);}
    if(++count==64){
      if(xQueueSend(inputs,&block,0)!=pdPASS){++queue_overflows;fail(9);ESP_LOGE("acquisition","ADC input queue full");}
      const uint32_t queued=uxQueueMessagesWaiting(inputs);
      if(queued>adc_input_highwater.load())adc_input_highwater=queued;
      count=0;
    }
  }
}
void process(void*) {
  Resampler filter;Input in{};WaveBlock out{};uint32_t e=0;unsigned count=0;uint64_t sequence=0,local=0,origin=0;int32_t values[8];
  for(;;){dsp_idle=true;if(xQueueReceive(inputs,&in,pdMS_TO_TICKS(10))!=pdPASS)continue;dsp_idle=false;
    if(!acquiring||in.epoch!=epoch)continue;
    if(e!=in.epoch){e=in.epoch;filter.reset();count=0;sequence=0;local=0;origin=in.stamp;}
    for(unsigned i=0;i<64&&acquiring;++i,++local){
      if(config.rate==10000){if(!filter.push(in.s[i],values))continue;}else memcpy(values,in.s[i],sizeof(values));
      if(!count){out.epoch=e;out.selectors=active;out.first=sequence;out.time_us=config.rate==10000?origin+(local*10+filter.phase()*2-475)*25/4:origin+local*1000000/config.rate;}
      memcpy(out.samples+count*8,values,sizeof(values));++sequence;
      if(++count==128){
        if(xQueueSend(wave_queue,&out,0)!=pdPASS){++queue_overflows;fail(9);ESP_LOGE("acquisition","Waveform output queue full");}
        count=0;
        // Stop the producer with ample headroom, then let the sender drain all
        // complete blocks. Restart in a new epoch so the pause is never hidden.
        if(uxQueueSpacesAvailable(wave_queue)<=256&&!fault)pause_transport();
      }
    }
  }
}
void controller(void*) {
  for(;;){
    if(reconfiguring||!receiver_ready||transport_paused||fault){safe_off();vTaskDelay(pdMS_TO_TICKS(20));continue;}
    if(acquiring){if(!hardware_ok()&&acquiring&&!transport_paused)fail(3);vTaskDelay(1);continue;}
    safe_off();gpio_intr_disable(GPIO_NUM_9);
    int waits=0;while((!adc_idle||!dsp_idle)&&waits++<100)vTaskDelay(1);
    if(!adc_idle||!dsp_idle){fail(12);continue;}
    xQueueReset(inputs);xQueueReset(wave_queue);active=selectors();
    if(active==3||!gpio_get_level(GPIO_NUM_2)||feedback()!=0){fail(1);continue;}
    vTaskDelay(pdMS_TO_TICKS(100));
    if(reconfiguring||selectors()!=active||!receiver_ready||transport_paused||fault)continue;
    gpio_set_level(GPIO_NUM_47,(active&1)?1:0);
    vTaskDelay(pdMS_TO_TICKS(100));
    if(!receiver_ready||transport_paused||fault){safe_off();continue;}
    if(!hardware_ok()){fail(3);continue;}
    if(!init_adc()){fail(4);continue;}
    if(!receiver_ready||transport_paused||fault||!hardware_ok()){safe_off();continue;}
    ++epoch;gpio_intr_enable(GPIO_NUM_9);acquiring=true;gpio_set_level(GPIO_NUM_17,1);
  }
}
}
uint8_t IRAM_ATTR selectors(){return gpio_get_level(GPIO_NUM_8)|(gpio_get_level(GPIO_NUM_21)<<1);}
uint8_t IRAM_ATTR feedback(){uint8_t f=0;for(int i=0;i<4;++i)if(gpio_get_level(gpio_num_t(38+i)))f|=1<<i;return f;}
void safe_off(){
  acquiring=false;gpio_set_level(GPIO_NUM_47,0);
  // The setup task owns RUN while unconfigured; do not erase its blink.
  if(!reset_feedback && (configured || reconfiguring || fault))gpio_set_level(GPIO_NUM_17,0);
}
bool acquisition_idle(){return !acquiring&&adc_idle&&dsp_idle;}
void pause_transport(const char *reason){
  const bool first=!transport_paused.exchange(true);
  safe_off();
  if(first){++transport_pauses;ESP_LOGW("acquisition","%s paused; preserving queued waveform blocks until receiver recovers",reason);}
}
void fail(uint32_t code){uint32_t empty=0;bool first=fault.compare_exchange_strong(empty,code);safe_off();gpio_set_level(GPIO_NUM_18,1);if(first)ESP_LOGE("acquisition","Stopped: fault=%lu selectors=%u feedback=%u",(unsigned long)code,selectors(),feedback());}
char *calibration_json() {
  cJSON *j=cJSON_CreateObject(),*slopes=cJSON_CreateArray(),*cal=cJSON_CreateArray();
  bool sct=selectors()&1;
  bool reference_board=!strcmp(node_id,"38F5F090A994");
  for(int ch=0;ch<4;++ch){double slope=sct?109.50585971309*1.2/8388608:reference_board?Calibration::ctSlopes[ch]:Board::nominalAmpsPerAdcMv*Board::mvPerCount;cJSON_AddItemToArray(slopes,cJSON_CreateNumber(slope));cJSON_AddItemToArray(cal,cJSON_CreateBool(!sct&&reference_board));}
  for(int ch=0;ch<3;++ch){cJSON_AddItemToArray(slopes,cJSON_CreateNumber(voltage[ch]));cJSON_AddItemToArray(cal,cJSON_CreateBool(voltage_cal[ch]));}
  cJSON_AddItemToArray(slopes,cJSON_CreateNumber(1200.0/8388608));cJSON_AddItemToArray(cal,cJSON_CreateBool(false));
  cJSON_AddItemToObject(j,"slopes",slopes);cJSON_AddItemToObject(j,"reference_calibrated",cal);
  cJSON_AddStringToObject(j,"ct_model",sct?"SCT013-030":"CR3110-3000");
  cJSON_AddStringToObject(j,"units","A,A,A,A,V,V,V,mV");
  char *text=cJSON_PrintUnformatted(j);cJSON_Delete(j);return text;
}
void acquisition_start() {
  for(unsigned i=0;i<256;++i){uint16_t c=i<<8;for(unsigned b=0;b<8;++b)c=(c&0x8000)?uint16_t((c<<1)^0x1021):uint16_t(c<<1);crc_table[i]=c;}
  for(int i=0;i<3;++i)voltage[i]=Calibration::voltageNominalSlopes[i];
  nvs_handle_t n;if(nvs_open("isov4c-r4-100",NVS_READONLY,&n)==ESP_OK){
    for(int i=0;i<3;++i){const char *key=i==0?"v4":i==1?"v5":"v6";uint8_t data[5];size_t sz=5;float factor;
      if(nvs_get_blob(n,key,data,&sz)==ESP_OK&&sz==5){memcpy(&factor,data+1,4);if(data[0]==1&&isfinite(factor)&&factor>0&&factor<=.05){voltage[i]=factor;voltage_cal[i]=true;}}
    }nvs_close(n);
  }
  if(!strcmp(config.link,"ble")) {
    // Queue copies run with interrupts masked. PSRAM copies can exceed the
    // 62.5 us native ADC period and cause a stale frame (fault 7). Keep this
    // queue internal, but limit it to 32 ms at 16 kSPS so BLE can also start.
    constexpr unsigned ble_input_blocks=8;
    auto *samples=(uint8_t*)heap_caps_malloc(ble_input_blocks*sizeof(Input),MALLOC_CAP_INTERNAL|MALLOC_CAP_8BIT);
    auto *queue=(StaticQueue_t*)heap_caps_malloc(sizeof(StaticQueue_t),MALLOC_CAP_INTERNAL|MALLOC_CAP_8BIT);
    if(!samples||!queue){heap_caps_free(samples);heap_caps_free(queue);fail(11);return;}
    inputs=xQueueCreateStatic(ble_input_blocks,sizeof(Input),samples,queue);
  } else inputs=xQueueCreate(32,sizeof(Input));
  // 13.1 s at 10 kSPS. Pause at 75% occupancy, retain complete waveform
  // blocks and drain them before starting a new acquisition epoch.
  constexpr unsigned buffered_blocks=1024;
  auto *store=(uint8_t*)heap_caps_malloc(buffered_blocks*sizeof(WaveBlock),MALLOC_CAP_SPIRAM|MALLOC_CAP_8BIT);
  auto *control=(StaticQueue_t*)heap_caps_malloc(sizeof(StaticQueue_t),MALLOC_CAP_INTERNAL|MALLOC_CAP_8BIT);
  if(!inputs||!store||!control){fail(11);return;}wave_queue=xQueueCreateStatic(buffered_blocks,sizeof(WaveBlock),store,control);
  spi_bus_config_t bus{};bus.mosi_io_num=11;bus.miso_io_num=13;bus.sclk_io_num=12;bus.quadwp_io_num=bus.quadhd_io_num=-1;bus.max_transfer_sz=30;
  // A 30-byte frame fits the FIFO; DMA adds allocation/copy work at 16 kHz.
  ESP_ERROR_CHECK(spi_bus_initialize(SPI2_HOST,&bus,SPI_DMA_DISABLED));
  spi_device_interface_config_t dev{};dev.clock_speed_hz=12000000;dev.mode=1;dev.spics_io_num=-1;dev.queue_size=1;
  ESP_ERROR_CHECK(spi_bus_add_device(SPI2_HOST,&dev,&adc));
  // At 16 kSPS the raw buffer holds 32 ms for BLE, 128 ms for TCP. Drain it
  // ahead of TCP/IP (priority 18) and Ethernet RX (15): DSP takes precedence.
  if(xTaskCreatePinnedToCore(acquire,"ADC",6144,nullptr,20,&adc_task,1)!=pdPASS||xTaskCreatePinnedToCore(process,"resampler",20480,nullptr,19,nullptr,0)!=pdPASS){fail(11);return;}
  ESP_ERROR_CHECK(gpio_set_intr_type(GPIO_NUM_9,GPIO_INTR_NEGEDGE));ESP_ERROR_CHECK(gpio_isr_handler_add(GPIO_NUM_9,ready,nullptr));gpio_intr_disable(GPIO_NUM_9);
  xTaskCreatePinnedToCore(controller,"interlocks",4096,nullptr,21,nullptr,1);
}
