#include "node.h"
#include "esp_http_server.h"
#include "esp_wifi.h"
#include "esp_system.h"
#include "esp_app_desc.h"
#include <string.h>
#include <stdlib.h>
#include <ctype.h>
extern const char page_start[] asm("_binary_index_html_start");
extern const char page_end[] asm("_binary_index_html_end");
extern const char logo_start[] asm("_binary_logo_png_start");
extern const char logo_end[] asm("_binary_logo_png_end");
namespace {
esp_err_t json(httpd_req_t *r,cJSON *j){char *text=cJSON_PrintUnformatted(j);cJSON_Delete(j);httpd_resp_set_type(r,"application/json");httpd_resp_set_hdr(r,"Cache-Control","no-store");auto e=httpd_resp_sendstr(r,text);free(text);return e;}
esp_err_t error(httpd_req_t*r,const char*why,const char*status="400 Bad Request"){httpd_resp_set_status(r,status);cJSON*j=cJSON_CreateObject();cJSON_AddStringToObject(j,"error",why);return json(r,j);}
// No setup login. A non-secret custom header prevents cross-origin browser forms
// from changing settings; this server deliberately grants no CORS access.
bool setup_request(httpd_req_t*r){char marker[8]={};return httpd_req_get_hdr_value_str(r,"X-Node-Setup",marker,sizeof(marker))==ESP_OK&&!strcmp(marker,"1");}
cJSON *body(httpd_req_t*r){if(r->content_len<2||r->content_len>3072)return nullptr;char buf[3073];int got=0;while(got<r->content_len){int n=httpd_req_recv(r,buf+got,r->content_len-got);if(n<=0)return nullptr;got+=n;}buf[got]=0;return cJSON_ParseWithLength(buf,got);}
void reboot(void*){vTaskDelay(pdMS_TO_TICKS(1000));safe_off();esp_restart();}
void ip(cJSON*j,const char *key,esp_netif_t*n){esp_netif_ip_info_t info{};char text[16];esp_netif_get_ip_info(n,&info);snprintf(text,sizeof(text),IPSTR,IP2STR(&info.ip));cJSON_AddStringToObject(j,key,text);}
esp_err_t status(httpd_req_t*r){
  cJSON*j=cJSON_CreateObject();cJSON_AddStringToObject(j,"device",node_id);cJSON_AddStringToObject(j,"name",ap_name);cJSON_AddStringToObject(j,"hostname",hostname);
  cJSON_AddBoolToObject(j,"configured",configured);cJSON_AddBoolToObject(j,"receiver",receiver_ready);cJSON_AddBoolToObject(j,"running",acquiring);
  cJSON_AddNumberToObject(j,"fault",fault);cJSON_AddNumberToObject(j,"blocks",sent_blocks);cJSON_AddNumberToObject(j,"overflows",queue_overflows);
  cJSON_AddStringToObject(j,"firmware",esp_app_get_description()->version);
  cJSON_AddBoolToObject(j,"transport_paused",transport_paused);
  cJSON_AddNumberToObject(j,"transport_pauses",transport_pauses);
  cJSON_AddNumberToObject(j,"queued_blocks",wave_queue?uxQueueMessagesWaiting(wave_queue):0);
  cJSON_AddNumberToObject(j,"adc_crc",adc_crc_errors);cJSON_AddNumberToObject(j,"missed_adc",missed_adc);cJSON_AddNumberToObject(j,"rate",config.rate);
  cJSON_AddStringToObject(j,"link",configured?config.link:"");cJSON_AddNumberToObject(j,"selectors",selectors());cJSON_AddNumberToObject(j,"feedback",feedback());
  cJSON_AddStringToObject(j,"ble_receiver",ble_receiver);
  wifi_ap_record_t ap{};if(esp_wifi_sta_get_ap_info(&ap)==ESP_OK)cJSON_AddStringToObject(j,"wifi_ssid",(char*)ap.ssid);
  ip(j,"ethernet_ip",eth_netif);ip(j,"wifi_ip",wifi_netif);return json(r,j);
}
esp_err_t scan(httpd_req_t*r){
  if(!setup_request(r))return error(r,"Open the node setup page","403 Forbidden");
  if(acquiring||receiver_ready||reconfiguring)return error(r,"Streaming is active","409 Conflict");
  wifi_scan_config_t sc{};if(esp_wifi_scan_start(&sc,true)!=ESP_OK)return error(r,"Wi-Fi scan is busy");
  uint16_t count=24;wifi_ap_record_t records[24];if(esp_wifi_scan_get_ap_records(&count,records)!=ESP_OK)return error(r,"Could not read networks. Scan again.");cJSON*j=cJSON_CreateArray();
  for(int i=0;i<count;++i){if(!records[i].ssid[0])continue;cJSON*a=cJSON_CreateObject();cJSON_AddStringToObject(a,"ssid",(char*)records[i].ssid);cJSON_AddNumberToObject(a,"rssi",records[i].rssi);cJSON_AddBoolToObject(a,"secured",records[i].authmode!=WIFI_AUTH_OPEN);cJSON_AddItemToArray(j,a);}return json(r,j);
}
esp_err_t connect(httpd_req_t*r){
  if(!setup_request(r))return error(r,"Open the node setup page","403 Forbidden");
  if(acquiring||receiver_ready||reconfiguring)return error(r,"Streaming is active","409 Conflict");
  cJSON*j=body(r);auto*s=cJSON_GetObjectItemCaseSensitive(j,"ssid");auto*p=cJSON_GetObjectItemCaseSensitive(j,"password");
  if(!cJSON_IsString(s)||!s->valuestring[0]||strlen(s->valuestring)>32||!cJSON_IsString(p)||strlen(p->valuestring)>64){cJSON_Delete(j);return error(r,"Check Wi-Fi details");}
  size_t length=strlen(p->valuestring);bool password_ok=!length||length>=8;
  if(length==64)for(size_t i=0;i<length;++i)if(!isxdigit((unsigned char)p->valuestring[i]))password_ok=false;
  if(!password_ok){cJSON_Delete(j);return error(r,"Wi-Fi password must be empty for open networks or 8–63 characters (or a 64-digit hex key)");}
  network_try_wifi(s->valuestring,p->valuestring);cJSON_Delete(j);return json(r,cJSON_CreateObject());
}
esp_err_t ble_devices(httpd_req_t*r){
  if(!setup_request(r))return error(r,"Open the node setup page","403 Forbidden");
  if(r->method==HTTP_POST){
    if(ble_scan_start()!=ESP_OK)return error(r,"BLE scan is unavailable while streaming or already scanning","409 Conflict");
  }
  return json(r,ble_scan_results());
}
esp_err_t ble_confirm(httpd_req_t*r){
  if(!setup_request(r))return error(r,"Open the node setup page","403 Forbidden");
  if(acquiring||receiver_ready||reconfiguring)return error(r,"Streaming is active","409 Conflict");
  cJSON*j=body(r),*address=cJSON_GetObjectItemCaseSensitive(j,"address");
  bool ok=cJSON_IsString(address)&&ble_receiver_available(address->valuestring);cJSON_Delete(j);
  if(!ok)return error(r,"Scan again and select an available MCSA receiver. Start its USB dashboard first.");
  return json(r,cJSON_CreateObject());
}
esp_err_t save(httpd_req_t*r){
  if(!setup_request(r))return error(r,"Open the node setup page","403 Forbidden");
  if(acquiring||receiver_ready||reconfiguring)return error(r,"Hold BOOT for 10 seconds to reopen setup","409 Conflict");
  cJSON*j=body(r);Config value;char why[128],receiver[18]={};bool valid=validate_config(j,value,why,sizeof(why));
  if(valid&&!strcmp(value.link,"ble")){
    cJSON*selected=cJSON_GetObjectItemCaseSensitive(j,"ble_receiver");
    valid=cJSON_IsString(selected)&&ble_receiver_available(selected->valuestring);
    if(valid)strlcpy(receiver,selected->valuestring,sizeof(receiver));
    else strlcpy(why,"Scan and confirm an available BLE receiver first",sizeof(why));
  }
  // The page can recognize an existing connection without re-entering its key.
  // Reuse credentials only for the station that is actually connected.
  if(valid&&!strcmp(value.link,"wifi")&&!value.wifi_password[0]){
    wifi_ap_record_t ap{};wifi_config_t station{};
    if(esp_wifi_sta_get_ap_info(&ap)==ESP_OK&&!strcmp((char*)ap.ssid,value.ssid)&&
       esp_wifi_get_config(WIFI_IF_STA,&station)==ESP_OK&&
       strnlen((char*)station.sta.ssid,sizeof(station.sta.ssid))==strlen(value.ssid)&&
       !memcmp(station.sta.ssid,value.ssid,strlen(value.ssid))){
      memcpy(value.wifi_password,station.sta.password,sizeof(station.sta.password));
      value.wifi_password[sizeof(value.wifi_password)-1]=0;
    }
  }
  cJSON_Delete(j);
  if(!valid)return error(r,why);
  reconfiguring=true;receiver_ready=false;safe_off();
  if(!config_save(value,receiver)){reconfiguring=false;return error(r,"Settings could not be saved","500 Internal Server Error");}
  auto result=json(r,cJSON_CreateObject());xTaskCreate(reboot,"restart",2048,nullptr,3,nullptr);return result;
}
esp_err_t root(httpd_req_t*r){httpd_resp_set_type(r,"text/html; charset=utf-8");httpd_resp_set_hdr(r,"Cache-Control","no-store");httpd_resp_set_hdr(r,"X-Content-Type-Options","nosniff");httpd_resp_set_hdr(r,"Content-Security-Policy","default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'");return httpd_resp_send(r,page_start,page_end-page_start-1);}
esp_err_t logo(httpd_req_t*r){httpd_resp_set_type(r,"image/png");httpd_resp_set_hdr(r,"Cache-Control","public, max-age=86400");httpd_resp_set_hdr(r,"X-Content-Type-Options","nosniff");return httpd_resp_send(r,logo_start,logo_end-logo_start);}
}
void portal_start(){httpd_config_t c=HTTPD_DEFAULT_CONFIG();c.stack_size=8192;c.max_uri_handlers=12;c.uri_match_fn=httpd_uri_match_wildcard;httpd_handle_t h;ESP_ERROR_CHECK(httpd_start(&h,&c));
  for(auto route:{httpd_uri_t{"/api/status",HTTP_GET,status,nullptr},httpd_uri_t{"/api/scan",HTTP_GET,scan,nullptr},httpd_uri_t{"/api/connect",HTTP_POST,connect,nullptr},httpd_uri_t{"/api/ble-devices",HTTP_GET,ble_devices,nullptr},httpd_uri_t{"/api/ble-devices",HTTP_POST,ble_devices,nullptr},httpd_uri_t{"/api/ble-confirm",HTTP_POST,ble_confirm,nullptr},httpd_uri_t{"/api/settings",HTTP_POST,save,nullptr},httpd_uri_t{"/logo.png",HTTP_GET,logo,nullptr},httpd_uri_t{"/*",HTTP_GET,root,nullptr}})ESP_ERROR_CHECK(httpd_register_uri_handler(h,&route));
}
