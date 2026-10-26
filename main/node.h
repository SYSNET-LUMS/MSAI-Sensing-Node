#pragma once
#include <stdint.h>
#include <atomic>
#include <initializer_list>
#include "esp_err.h"
#include "esp_netif.h"
#include "cJSON.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"

struct Config {
  uint32_t version=2;
  char link[12]="ethernet", destination[12]="local";
  char ssid[33]={}, wifi_password[65]={};
  char host[254]={};
  uint16_t port=5443;
  uint32_t rate=10000;
  // Reserved for compatibility with saved v2 settings; this release uses DHCP.
  bool static_ip=false;
  char ip[16]={}, mask[16]={}, gateway[16]={}, dns[16]={};
};
struct WaveBlock { uint64_t first, time_us; uint32_t epoch, selectors; int32_t samples[128*8]; };
extern Config config;
extern char hotspot_password[9];
extern char node_id[13],ap_name[32],hostname[32];
extern char ble_receiver[18];
extern std::atomic<bool> configured, link_ready, receiver_ready, acquiring;
extern std::atomic<bool> reconfiguring;
extern std::atomic<bool> reset_feedback;
extern std::atomic<bool> transport_paused;
extern std::atomic<uint32_t> transport_pauses;
extern std::atomic<uint32_t> wifi_disconnects, wifi_disconnect_reason;
extern std::atomic<uint32_t> fault, epoch, sent_blocks, queue_overflows, missed_adc, adc_crc_errors;
extern std::atomic<uint32_t> adc_recoveries;
extern std::atomic<uint32_t> adc_input_highwater;
extern QueueHandle_t wave_queue;
extern esp_netif_t *wifi_netif,*eth_netif;
uint8_t selectors();
uint8_t feedback();
void safe_off();
void pause_transport(const char *reason="Transport");
bool acquisition_idle();
void fail(uint32_t code);
void config_init();
bool validate_config(const cJSON *body,Config &out,char *error,size_t size);
bool config_save(const Config &value,const char *receiver=nullptr);
bool config_reset();
void network_start();
void network_try_wifi(const char *ssid,const char *password);
void portal_start();
void acquisition_start();
void transport_start();
void ble_start();
esp_err_t ble_scan_start();
cJSON *ble_scan_results();
bool ble_receiver_available(const char *address);
bool ble_send(const uint8_t *data,size_t size);
char *calibration_json();
void finish_setup_ap();
