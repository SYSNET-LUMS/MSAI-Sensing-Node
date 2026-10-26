#include "node.h"
#include "esp_timer.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "nimble/nimble_port.h"
#include "nimble/nimble_port_freertos.h"
#include "host/ble_hs.h"
#include "services/gap/ble_svc_gap.h"
#include "services/gatt/ble_svc_gatt.h"
#include <string.h>
#include <stdlib.h>
#include <algorithm>
namespace {
// d0be1001/2/3-63f0-4dd1-a28d-e0201450a001 (service, data, control).
const ble_uuid128_t service=BLE_UUID128_INIT(0x01,0xa0,0x50,0x14,0x20,0xe0,0x8d,0xa2,0xd1,0x4d,0xf0,0x63,0x01,0x10,0xbe,0xd0);
const ble_uuid128_t data_id=BLE_UUID128_INIT(0x01,0xa0,0x50,0x14,0x20,0xe0,0x8d,0xa2,0xd1,0x4d,0xf0,0x63,0x02,0x10,0xbe,0xd0);
const ble_uuid128_t control_id=BLE_UUID128_INIT(0x01,0xa0,0x50,0x14,0x20,0xe0,0x8d,0xa2,0xd1,0x4d,0xf0,0x63,0x03,0x10,0xbe,0xd0);
const uint8_t base_id[16]={0x01,0xa0,0x50,0x14,0x20,0xe0,0x8d,0xa2,0xd1,0x4d,0xf0,0x63,0x04,0x10,0xbe,0xd0};
struct Found {ble_addr_t address;char name[32];int8_t rssi;bool compatible,ready;int64_t seen;};
Found discovered[32]{};unsigned discovered_count=0;
portMUX_TYPE discovered_lock=portMUX_INITIALIZER_UNLOCKED;
std::atomic<bool> initialized{false},synced{false},setup_scan{false},scanning{false};
void address_text(const ble_addr_t &a,char *text){snprintf(text,18,"%02X:%02X:%02X:%02X:%02X:%02X",a.val[5],a.val[4],a.val[3],a.val[2],a.val[1],a.val[0]);}
bool selected_address(uint8_t *out){
  unsigned v[6];int end=0;
  if(sscanf(ble_receiver,"%2x:%2x:%2x:%2x:%2x:%2x%n",v,v+1,v+2,v+3,v+4,v+5,&end)!=6||end!=17)return false;
  for(int i=0;i<6;++i)out[i]=v[5-i];
  return true;
}
int discovery_event(ble_gap_event*e,void*){
  if(e->type==BLE_GAP_EVENT_DISC_COMPLETE){scanning=false;return 0;}
  if(e->type!=BLE_GAP_EVENT_DISC)return 0;
  ble_hs_adv_fields fields{};if(ble_hs_adv_parse_fields(&fields,e->disc.data,e->disc.length_data))return 0;
  const bool compatible=e->disc.addr.type==BLE_ADDR_PUBLIC&&fields.svc_data_uuid128_len==18&&
    !memcmp(fields.svc_data_uuid128,base_id,16)&&fields.svc_data_uuid128[16]==1;
  portENTER_CRITICAL(&discovered_lock);
  unsigned index=0;while(index<discovered_count&&memcmp(&discovered[index].address,&e->disc.addr,sizeof(ble_addr_t)))++index;
  if(index==discovered_count){
    if(discovered_count<32)++discovered_count;
    else if(compatible){index=0;while(index<32&&discovered[index].compatible)++index;}
    if(index<32){discovered[index]={};discovered[index].address=e->disc.addr;}
  }
  if(index<32){
    Found &f=discovered[index];f.rssi=e->disc.rssi;f.seen=esp_timer_get_time();
    if(fields.name&&fields.name_len){size_t n=std::min(size_t(fields.name_len),sizeof(f.name)-1);memcpy(f.name,fields.name,n);f.name[n]=0;}
    if(compatible){f.compatible=true;f.ready=fields.svc_data_uuid128[17]&1;}
  }
  portEXIT_CRITICAL(&discovered_lock);return 0;
}
std::atomic<uint16_t> connection{BLE_HS_CONN_HANDLE_NONE};uint16_t data_handle;
uint8_t addr_type;
std::atomic<bool> subscribed{false},streaming{false};
std::atomic<uint32_t> generation{0};
std::atomic<uint64_t> acknowledged{UINT64_MAX},pending{UINT64_MAX};
std::atomic<unsigned> window{1};
uint64_t counter=0;
void stop(){streaming=false;++generation;receiver_ready=false;safe_off();}
int access(uint16_t,uint16_t,ble_gatt_access_ctxt*ctx,void*){
  if(reconfiguring)return BLE_ATT_ERR_UNLIKELY;
  if(ctx->op==BLE_GATT_ACCESS_OP_READ_CHR){
    char*cal=calibration_json();if(!cal)return BLE_ATT_ERR_INSUFFICIENT_RES;
    char text[1400];int n=snprintf(text,sizeof(text),"{\"protocol\":\"MSAC1\",\"transport\":\"ble-plain-v1\",\"ble_window_max\":8,\"device\":\"%s\",\"rate\":%lu,\"calibration\":%s}",node_id,(unsigned long)config.rate,cal);free(cal);
    return n>0&&size_t(n)<sizeof(text)&&os_mbuf_append(ctx->om,text,n)==0?0:BLE_ATT_ERR_INSUFFICIENT_RES;
  }
  uint8_t value[8];uint16_t size=0;
  if(ble_hs_mbuf_to_flat(ctx->om,value,sizeof(value),&size))return BLE_ATT_ERR_INVALID_ATTR_VALUE_LEN;
  if(size==4&&!memcmp(value,"WIN8",4)&&!streaming){window=8;return 0;}
  if(size==5&&!memcmp(value,"START",5)&&!streaming){
    if(!subscribed||ble_att_mtu(connection)<100)return BLE_ATT_ERR_UNLIKELY;
    ESP_LOGI("ble","START mtu=%u host_stack_free=%u",unsigned(ble_att_mtu(connection)),unsigned(uxTaskGetStackHighWaterMark(nullptr)));
    counter=0;acknowledged=UINT64_MAX;pending=UINT64_MAX;++generation;
    streaming=true;receiver_ready=true;finish_setup_ap();return 0;
  }
  if(size==4&&!memcmp(value,"STOP",4)){stop();return 0;}
  if(size==8&&streaming){
    uint64_t number=0;for(int i=0;i<8;++i)number|=uint64_t(value[i])<<(8*i);
    uint64_t sent=pending.load(),previous=acknowledged.load();
    if(number==UINT64_MAX||sent==UINT64_MAX||number>sent||(previous!=UINT64_MAX&&number<previous))return BLE_ATT_ERR_UNLIKELY;
    acknowledged=number;return 0;
  }
  return BLE_ATT_ERR_UNLIKELY;
}
ble_gatt_chr_def chars[3];ble_gatt_svc_def services[2];
void advertise();
int event(ble_gap_event*e,void*){
  if(e->type==BLE_GAP_EVENT_CONNECT){
    if(e->connect.status){advertise();return 0;}
    ble_gap_conn_desc peer{};uint8_t selected[6];
    if(setup_scan||(ble_receiver[0]&&(!selected_address(selected)||ble_gap_conn_find(e->connect.conn_handle,&peer)||peer.peer_id_addr.type!=BLE_ADDR_PUBLIC||memcmp(selected,peer.peer_id_addr.val,6)))){
      ble_gap_terminate(e->connect.conn_handle,BLE_ERR_REM_USER_CONN_TERM);return 0;
    }
    connection=e->connect.conn_handle;subscribed=false;window=1;stop();ESP_LOGI("ble","Connected");
  }
  if(e->type==BLE_GAP_EVENT_DISCONNECT){ESP_LOGI("ble","Disconnected: reason=%d",e->disconnect.reason);connection=BLE_HS_CONN_HANDLE_NONE;subscribed=false;stop();advertise();}
  if(e->type==BLE_GAP_EVENT_SUBSCRIBE&&e->subscribe.attr_handle==data_handle){subscribed=e->subscribe.cur_notify;if(!subscribed)stop();}
  return 0;
}
void advertise(){
  if(setup_scan||reconfiguring||!configured||strcmp(config.link,"ble"))return;
  ble_hs_adv_fields fields{};fields.flags=BLE_HS_ADV_F_DISC_GEN|BLE_HS_ADV_F_BREDR_UNSUP;fields.uuids128=(ble_uuid128_t*)&service;fields.num_uuids128=1;fields.uuids128_is_complete=1;
  uint8_t selected[8]={0xff,0xff};
  if(ble_receiver[0]&&!selected_address(selected+2))return;
  if(selected_address(selected+2)){fields.mfg_data=selected;fields.mfg_data_len=sizeof(selected);}
  ESP_ERROR_CHECK(ble_gap_adv_set_fields(&fields));
  ble_hs_adv_fields scan{};scan.name=(uint8_t*)ap_name;scan.name_len=strlen(ap_name);scan.name_is_complete=1;ESP_ERROR_CHECK(ble_gap_adv_rsp_set_fields(&scan));
  ble_gap_adv_params p{};p.conn_mode=BLE_GAP_CONN_MODE_UND;p.disc_mode=BLE_GAP_DISC_MODE_GEN;ESP_ERROR_CHECK(ble_gap_adv_start(addr_type,nullptr,BLE_HS_FOREVER,&p,event,nullptr));
  ESP_LOGI("ble","Advertising as %s",ap_name);
}
void ble_synced(){ESP_ERROR_CHECK(ble_hs_id_infer_auto(0,&addr_type));synced=true;advertise();}
void run(void*){nimble_port_run();nimble_port_freertos_deinit();}
}
void ble_start(){
  if(initialized.exchange(true))return;
  ESP_LOGI("ble","Starting: internal free=%u largest=%u",unsigned(heap_caps_get_free_size(MALLOC_CAP_INTERNAL|MALLOC_CAP_8BIT)),unsigned(heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL|MALLOC_CAP_8BIT)));
  ESP_ERROR_CHECK(nimble_port_init());ble_hs_cfg.sync_cb=ble_synced;ble_svc_gap_init();ble_svc_gatt_init();ble_svc_gap_device_name_set(ap_name);
  chars[0].uuid=&data_id.u;chars[0].flags=BLE_GATT_CHR_F_NOTIFY;chars[0].val_handle=&data_handle;chars[0].access_cb=access;
  chars[1].uuid=&control_id.u;chars[1].flags=BLE_GATT_CHR_F_READ|BLE_GATT_CHR_F_WRITE|BLE_GATT_CHR_F_WRITE_NO_RSP;chars[1].access_cb=access;
  services[0].type=BLE_GATT_SVC_TYPE_PRIMARY;services[0].uuid=&service.u;services[0].characteristics=chars;
  ESP_ERROR_CHECK(ble_gatts_count_cfg(services));ESP_ERROR_CHECK(ble_gatts_add_svcs(services));nimble_port_freertos_init(run);
}
esp_err_t ble_scan_start(){
  if(acquiring||receiver_ready||reconfiguring||scanning||connection!=BLE_HS_CONN_HANDLE_NONE)return ESP_ERR_INVALID_STATE;
  setup_scan=true;ble_start();
  for(unsigned i=0;i<60&&!synced;++i)vTaskDelay(pdMS_TO_TICKS(50));
  if(!synced)return ESP_ERR_TIMEOUT;
  if(ble_gap_adv_active())ble_gap_adv_stop();
  portENTER_CRITICAL(&discovered_lock);discovered_count=0;portEXIT_CRITICAL(&discovered_lock);
  ble_gap_disc_params p{};p.itvl=80;p.window=48;p.passive=0;p.filter_duplicates=0;
  scanning=true;int rc=ble_gap_disc(addr_type,4000,&p,discovery_event,nullptr);
  if(rc){scanning=false;return ESP_FAIL;}return ESP_OK;
}
cJSON *ble_scan_results(){
  Found copy[32];unsigned count;
  portENTER_CRITICAL(&discovered_lock);count=discovered_count;memcpy(copy,discovered,count*sizeof(Found));portEXIT_CRITICAL(&discovered_lock);
  cJSON*j=cJSON_CreateObject(),*list=cJSON_AddArrayToObject(j,"devices");cJSON_AddBoolToObject(j,"scanning",scanning);
  for(unsigned i=0;i<count;++i){const Found &f=copy[i];char address[18];address_text(f.address,address);
    cJSON*d=cJSON_CreateObject();cJSON_AddStringToObject(d,"address",address);cJSON_AddStringToObject(d,"name",f.name[0]?f.name:"Unnamed BLE device");
    cJSON_AddNumberToObject(d,"address_type",f.address.type);cJSON_AddNumberToObject(d,"rssi",f.rssi);
    cJSON_AddBoolToObject(d,"compatible",f.compatible);cJSON_AddBoolToObject(d,"ready",f.compatible&&f.ready);cJSON_AddItemToArray(list,d);
  }return j;
}
bool ble_receiver_available(const char *address){
  if(!address||strlen(address)!=17)return false;
  bool valid=false;int64_t now=esp_timer_get_time();
  portENTER_CRITICAL(&discovered_lock);
  for(unsigned i=0;i<discovered_count;++i){char text[18];address_text(discovered[i].address,text);
    if(!strcmp(address,text)&&discovered[i].compatible&&discovered[i].ready&&now-discovered[i].seen<300000000){valid=true;break;}}
  portEXIT_CRITICAL(&discovered_lock);return valid;
}
bool ble_send(const uint8_t*data,size_t size){
  if(!streaming||!subscribed||size<44||size>3644)return false;
  uint16_t conn=connection;uint32_t session=generation;uint64_t number=counter++;
  auto active=[&](){return streaming&&subscribed&&connection==conn&&generation==session&&!reconfiguring;};
  if(number>UINT32_MAX){stop();ble_gap_terminate(conn,BLE_ERR_REM_USER_CONN_TERM);return false;}
  size_t offset=0;int64_t deadline=esp_timer_get_time()+3000000;
  // Credits are returned only after laptop validation. Older receivers retain
  // the original one-block stop-and-wait behavior unless WIN8 is negotiated.
  while(active()&&number-(acknowledged.load()+1)>=window&&esp_timer_get_time()<deadline)vTaskDelay(1);
  if(!active()||number-(acknowledged.load()+1)>=window){
    if(generation==session){stop();ble_gap_terminate(conn,BLE_ERR_REM_USER_CONN_TERM);}return false;
  }
  pending=number;
  while(offset<size&&active()&&esp_timer_get_time()<deadline){
    unsigned mtu=ble_att_mtu(conn);if(mtu<100)break;
    uint8_t piece[520];size_t n=std::min(size-offset,std::min(size_t(mtu-11),sizeof(piece)-8));
    for(int i=0;i<4;++i)piece[i]=number>>(8*i);
    piece[4]=offset;piece[5]=offset>>8;piece[6]=size;piece[7]=size>>8;memcpy(piece+8,data+offset,n);
    os_mbuf*m=ble_hs_mbuf_from_flat(piece,n+8);if(!m){vTaskDelay(1);continue;}
    int rc=ble_gatts_notify_custom(conn,data_handle,m);
    if(rc){vTaskDelay(1);continue;}offset+=n;
  }
  if(window==1)while(active()&&acknowledged!=number&&esp_timer_get_time()<deadline)vTaskDelay(1);
  if(active()&&offset==size&&(window>1||acknowledged==number))return true;
  if(generation==session){stop();if(conn!=BLE_HS_CONN_HANDLE_NONE)ble_gap_terminate(conn,BLE_ERR_REM_USER_CONN_TERM);}
  return false;
}
