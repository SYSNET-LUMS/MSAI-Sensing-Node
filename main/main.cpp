#include "node.h"
#include "driver/gpio.h"
#include "esp_system.h"
#include "esp_app_desc.h"
#include "esp_log.h"
#include "esp_intr_alloc.h"
#include "reset_button.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
static void console(void*){
  char line[40];unsigned length=0;
  for(;;){
    int c=getchar();if(c<0){vTaskDelay(pdMS_TO_TICKS(50));continue;}
    if(c=='\r'||c=='\n'){
      line[length]=0;
      if(!strcmp(line,"status")){
        esp_netif_ip_info_t eth{},wifi{};esp_netif_get_ip_info(eth_netif,&eth);esp_netif_get_ip_info(wifi_netif,&wifi);
        printf("NODE_STATUS configured=%u receiver=%u running=%u fault=%lu selectors=%u feedback=%u ethernet=" IPSTR " wifi=" IPSTR " blocks=%lu epoch=%lu missed_adc=%lu adc_recoveries=%lu adc_input_highwater=%lu queued=%u firmware=%s\n",unsigned(configured.load()),unsigned(receiver_ready.load()),unsigned(acquiring.load()),(unsigned long)fault.load(),selectors(),feedback(),IP2STR(&eth.ip),IP2STR(&wifi.ip),(unsigned long)sent_blocks.load(),(unsigned long)epoch.load(),(unsigned long)missed_adc.load(),(unsigned long)adc_recoveries.load(),(unsigned long)adc_input_highwater.load(),unsigned(wave_queue?uxQueueMessagesWaiting(wave_queue):0),esp_app_get_description()->version);
      }else if(!strcmp(line,"setup"))printf("SETUP ssid=%s password=%s url=http://192.168.4.1/ lan_url=http://%s.local/\n",ap_name,hotspot_password,hostname);
      else if(!strcmp(line,"ble-scan")){
        esp_err_t result=ble_scan_start();
        if(result!=ESP_OK)printf("BLE_SCAN_ERROR %s\n",esp_err_to_name(result));
        else {vTaskDelay(pdMS_TO_TICKS(4500));cJSON*j=ble_scan_results();char*text=cJSON_PrintUnformatted(j);if(text){printf("BLE_DEVICES %s\n",text);free(text);}cJSON_Delete(j);}
      }
      else if(!strncmp(line,"ble-select ",11)){
        char address[18];unsigned rate=0;int end=0;
        if(acquiring||receiver_ready||reconfiguring||sscanf(line+11,"%17s %u%n",address,&rate,&end)!=2||size_t(end)!=strlen(line+11)||(rate!=1000&&rate!=2000&&rate!=10000)||!ble_receiver_available(address))printf("BLE_SELECT_ERROR scan and select an available receiver first\n");
        else {Config value;strcpy(value.link,"ble");strcpy(value.destination,"local");value.rate=rate;
          if(config_save(value,address)){reconfiguring=true;safe_off();printf("BLE_SELECTED %s rate=%u\n",address,rate);vTaskDelay(pdMS_TO_TICKS(300));esp_restart();}
          else printf("BLE_SELECT_ERROR save failed\n");}
      }
      else if(!strcmp(line,"reset-setup")){if(config_reset())esp_restart();}
      else if(length)printf("Commands: status | setup | reset-setup | ble-scan | ble-select ADDRESS RATE\n");
      length=0;
    }else if(length<sizeof(line)-1)line[length++]=char(c);
  }
}
static void reset_button(void*){
  ResetButton button;
  for(;;){
    const auto action=button.update(!gpio_get_level(GPIO_NUM_0),uint32_t(xTaskGetTickCount()*portTICK_PERIOD_MS));
    if(action==ResetButton::Action::Clear){
      bool cleared=config_reset();button.complete(cleared);
      if(cleared){
        reset_feedback=true;
        gpio_set_level(GPIO_NUM_17,1);gpio_set_level(GPIO_NUM_18,1);
        ESP_LOGI("setup","SETUP_RESET_DONE both LEDs on; release BOOT to restart");
      }else{gpio_set_level(GPIO_NUM_18,1);}
    }else if(action==ResetButton::Action::Restart){
      gpio_set_level(GPIO_NUM_17,0);gpio_set_level(GPIO_NUM_18,0);
      ESP_LOGI("setup","BOOT released; restarting setup");
      esp_restart();
    }
    // Fresh setup blinks both LEDs together; a missing receiver blinks ALERT.
    // Fault and reset feedback take precedence over these heartbeats.
    if(!reset_feedback && !reconfiguring){
      const uint32_t now_ms=xTaskGetTickCount()*portTICK_PERIOD_MS;
      const bool setup=!configured;
      const bool has_fault=fault.load()!=0;
      const bool slow_blink=(now_ms/500)%2==0;
      const bool waiting=setup || !receiver_ready;
      const bool alert_blink=waiting?slow_blink:(transport_paused && (now_ms/250)%2==0);
      if(setup)gpio_set_level(GPIO_NUM_17,!has_fault && slow_blink);
      gpio_set_level(GPIO_NUM_18,has_fault || alert_blink);
    }
    vTaskDelay(pdMS_TO_TICKS(20));
  }
}
static void start_network(void *caller){
  // Wi-Fi peripheral interrupts belong on the networking core, not the ADC core.
  network_start();
  xTaskNotifyGive(static_cast<TaskHandle_t>(caller));
  vTaskDelete(nullptr);
}
extern "C" void app_main(){
  gpio_set_level(GPIO_NUM_47,0);gpio_set_direction(GPIO_NUM_47,GPIO_MODE_OUTPUT);
  for(int pin:{14,15,17,18}){gpio_set_level(gpio_num_t(pin),0);gpio_set_direction(gpio_num_t(pin),GPIO_MODE_OUTPUT);}
  for(int pin:{7,10}){gpio_set_level(gpio_num_t(pin),1);gpio_set_direction(gpio_num_t(pin),GPIO_MODE_OUTPUT);}
  for(int pin:{0,2,8,9,16,21,38,39,40,41})gpio_set_direction(gpio_num_t(pin),GPIO_MODE_INPUT);
  // app_main is pinned to core 1; keep data-ready interrupts with the ADC task.
  ESP_ERROR_CHECK(gpio_install_isr_service(ESP_INTR_FLAG_IRAM|ESP_INTR_FLAG_LEVEL3));
  config_init();
  xTaskCreate(reset_button,"setupReset",3072,nullptr,3,nullptr);
  printf("\nMCSA NODE %s | setup hotspot %s | password %s\n",node_id,ap_name,hotspot_password);
  printf("Setup: http://192.168.4.1/ or http://%s.local/ on the LAN\n",hostname);
  if(xTaskCreatePinnedToCore(start_network,"networkInit",8192,xTaskGetCurrentTaskHandle(),5,nullptr,0)!=pdPASS)abort();
  ulTaskNotifyTake(pdTRUE,portMAX_DELAY);
  portal_start();acquisition_start();
  // BLE setup diagnostics initialize the host and serialize the discovery table.
  if(xTaskCreate(console,"USBconsole",8192,nullptr,1,nullptr)!=pdPASS)abort();
  if(configured&&wave_queue)transport_start();
}
