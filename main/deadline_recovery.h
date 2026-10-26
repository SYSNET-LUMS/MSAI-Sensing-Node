#pragma once
#include <stdint.h>

// Only a short, isolated scheduling gap is recoverable. Never join samples
// across it: the caller must stop, drain complete blocks and change epoch.
class DeadlineRecovery {
  bool recovered_ = false;
  uint64_t last_us_ = 0;
public:
  constexpr bool allow(uint32_t edges, int64_t interval_us, uint64_t now_us, uint32_t native_rate) {
    if (!native_rate || edges < 2 || edges > 4 || interval_us <= 0 ||
        uint64_t(interval_us) > 8000000ULL / native_rate) return false;
    if (recovered_ && now_us - last_us_ < 60000000ULL) return false;
    recovered_ = true;
    last_us_ = now_us;
    return true;
  }
};
