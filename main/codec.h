#pragma once
#include <stddef.h>
#include <stdint.h>
// Channel-major first differences, ZigZag and adaptive Rice coding.
// Every block is independently decodable. Input is signed 24-bit, interleaved.
size_t msai_encode(const int32_t *samples,unsigned count,unsigned channels,uint8_t *out,size_t capacity);
bool msai_decode(const uint8_t *data,size_t size,unsigned count,unsigned channels,int32_t *out);
uint32_t msai_crc32(const uint8_t *data,size_t size);
