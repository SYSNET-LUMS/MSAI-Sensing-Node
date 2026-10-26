#include "node.h"
#include "nvs_flash.h"
#include "nvs.h"
#include "esp_mac.h"
#include <stdio.h>
#include <string.h>
#include <ctype.h>

Config config;
char node_id[13],ap_name[32],hostname[32];
char ble_receiver[18]={};
char hotspot_password[9];
std::atomic<bool> configured{false},link_ready{false},receiver_ready{false},acquiring{false};
std::atomic<bool> reconfiguring{false};
std::atomic<bool> reset_feedback{false};
std::atomic<bool> transport_paused{false};
std::atomic<uint32_t> transport_pauses{0};
std::atomic<uint32_t> fault{0},epoch{0},sent_blocks{0},queue_overflows{0},missed_adc{0},adc_crc_errors{0};
std::atomic<uint32_t> adc_recoveries{0};
std::atomic<uint32_t> adc_input_highwater{0};
QueueHandle_t wave_queue=nullptr;

namespace {
void use_dhcp(Config &value){
  value.static_ip=false;
  memset(value.ip,0,sizeof(value.ip));memset(value.mask,0,sizeof(value.mask));
  memset(value.gateway,0,sizeof(value.gateway));memset(value.dns,0,sizeof(value.dns));
}
// Read the previous NVS layout once, retaining network settings but dropping
// receiver credentials. Sensor calibration uses separate, unchanged namespaces.
struct LegacyConfigV1 {
  uint32_t version;
  char link[12],destination[12],ssid[33],wifi_password[65];
  char host[254],token[65],pin[65];
  uint16_t port;
  uint32_t rate;
  bool static_ip;
  char ip[16],mask[16],gateway[16],dns[16];
};
}

void config_init() {
  // Never erase this partition on init failure: existing sensor calibration lives here.
  ESP_ERROR_CHECK(nvs_flash_init());
  uint8_t mac[6]; ESP_ERROR_CHECK(esp_read_mac(mac,ESP_MAC_WIFI_STA));
  snprintf(node_id,sizeof(node_id),"%02X%02X%02X%02X%02X%02X",mac[5],mac[4],mac[3],mac[2],mac[1],mac[0]);
  // Use the actual Wi-Fi MAC suffix; keep the existing wire device ID unchanged.
  snprintf(ap_name,sizeof(ap_name),"SENSING NODE %02X%02X",mac[4],mac[5]);
  snprintf(hotspot_password,sizeof(hotspot_password),"MCSA%02X%02X",mac[4],mac[5]);
  snprintf(hostname,sizeof(hostname),"mcsa%02x%02x",mac[4],mac[5]);
  nvs_handle_t n;
  ESP_ERROR_CHECK(nvs_open("msai-network",NVS_READWRITE,&n));
  size_t size=0; bool migrated=false;
  if(nvs_get_blob(n,"config",nullptr,&size)==ESP_OK) {
    if(size==sizeof(Config)) {
      Config saved;
      if(nvs_get_blob(n,"config",&saved,&size)==ESP_OK&&saved.version==2) {config=saved;configured=true;}
    } else if(size==sizeof(LegacyConfigV1)) {
      LegacyConfigV1 old{};
      if(nvs_get_blob(n,"config",&old,&size)==ESP_OK&&old.version==1) {
        auto copy=[](auto &dst,const auto &src){memcpy(dst,src,sizeof(dst));dst[sizeof(dst)-1]=0;};
        copy(config.link,old.link);copy(config.destination,old.destination);
        copy(config.ssid,old.ssid);copy(config.wifi_password,old.wifi_password);copy(config.host,old.host);
        copy(config.ip,old.ip);copy(config.mask,old.mask);copy(config.gateway,old.gateway);copy(config.dns,old.dns);
        config.port=old.port;config.rate=old.rate;config.static_ip=old.static_ip;
        configured=true;migrated=true;
      }
    }
  }
  size_t receiver_size=sizeof(ble_receiver);
  if(nvs_get_str(n,"ble_receiver",ble_receiver,&receiver_size)!=ESP_OK)ble_receiver[0]=0;
  nvs_close(n);
  if(config.static_ip||config.ip[0]||config.mask[0]||config.gateway[0]||config.dns[0])migrated=true;
  use_dhcp(config);
  if(migrated&&!config_save(config)) printf("Network settings migration could not be saved\n");
}
bool config_save(const Config &value,const char *receiver) {
  nvs_handle_t n; if(nvs_open("msai-network",NVS_READWRITE,&n)!=ESP_OK) return false;
  Config automatic=value;use_dhcp(automatic);
  bool ok=nvs_set_blob(n,"config",&automatic,sizeof(automatic))==ESP_OK;
  if(ok&&receiver)ok=nvs_set_str(n,"ble_receiver",receiver)==ESP_OK;
  if(ok)ok=nvs_commit(n)==ESP_OK;
  nvs_close(n); return ok;
}
bool config_reset() {
  reconfiguring=true; safe_off(); receiver_ready=false;
  nvs_handle_t n;esp_err_t result=nvs_open("msai-network",NVS_READWRITE,&n);
  if(result==ESP_OK) {result=nvs_erase_all(n);if(result==ESP_OK)result=nvs_commit(n);nvs_close(n);}
  // Legacy sensor calibration and other namespaces are retained.
  if(result!=ESP_OK){printf("SETUP_RESET_FAILED %s; restart and retry\n",esp_err_to_name(result));return false;}
  configured=false;return true;
}
bool validate_config(const cJSON *j,Config &o,char *error,size_t cap) {
  auto bad=[&](const char *why){snprintf(error,cap,"%s",why);return false;};
  auto str=[&](const char *key,char *dest,size_t len,bool required){
    const cJSON *v=cJSON_GetObjectItemCaseSensitive(j,key);
    if(!v) return !required;
    if(!cJSON_IsString(v)||strlen(v->valuestring)>=len) return false;
    for(const unsigned char *p=(const unsigned char*)v->valuestring;*p;++p) if(*p<32||*p==127) return false;
    strcpy(dest,v->valuestring); return !required||dest[0];
  };
  if(!cJSON_IsObject(j)) return bad("Invalid settings");
  if(!str("link",o.link,sizeof(o.link),true)||
    (strcmp(o.link,"wifi")&&strcmp(o.link,"ethernet")&&strcmp(o.link,"ble"))) return bad("Choose a connection");
  bool ble=!strcmp(o.link,"ble"),wifi=!strcmp(o.link,"wifi");
  if(wifi&&(!str("ssid",o.ssid,sizeof(o.ssid),true)||!str("wifi_password",o.wifi_password,sizeof(o.wifi_password),false))) return bad("Check Wi-Fi details");
  if(wifi){size_t n=strlen(o.wifi_password);if(n&&n<8)return bad("Wi-Fi password is too short");if(n==64)for(size_t i=0;i<n;++i)if(!isxdigit((unsigned char)o.wifi_password[i]))return bad("Invalid Wi-Fi hex key");}
  if(!str("destination",o.destination,sizeof(o.destination),true)||
    (strcmp(o.destination,"local")&&strcmp(o.destination,"cloud"))||(ble&&strcmp(o.destination,"local"))) return bad("BLE uses a local base station");
  if(!ble) {
    if(!str("host",o.host,sizeof(o.host),true)||strchr(o.host,'/')||strchr(o.host,':')||strchr(o.host,' ')) return bad("Enter an IPv4 address or hostname, without a URL path");
    const cJSON *port=cJSON_GetObjectItemCaseSensitive(j,"port");
    if(!cJSON_IsNumber(port)||port->valuedouble!=port->valueint||port->valueint<1||port->valueint>65535) return bad("Invalid port");
    o.port=port->valueint;
  }
  const cJSON *rate=cJSON_GetObjectItemCaseSensitive(j,"rate");
  if(!cJSON_IsNumber(rate)||rate->valuedouble!=rate->valueint) return bad("Choose a sample rate");
  o.rate=rate->valueint;
  if((!ble&&o.rate!=10000)||(ble&&o.rate!=1000&&o.rate!=2000&&o.rate!=10000)) return bad("Unsupported sample rate");
  if(cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(j,"static_ip")))return bad("Static IP is unavailable. Use automatic addressing.");
  use_dhcp(o);
  return true;
}
