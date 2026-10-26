#pragma once
#include <stdint.h>
#include <math.h>
#include <string.h>
#include "filter_coefficients.h"

// Causal 5/8 rational resampler: 16 kSPS -> 10 kSPS on every channel.
// 96 input taps, five fractional-delay phases, 4 kHz cutoff, Blackman window.
// Same coefficients/timing on all channels; common delay = 47.5/16000 s.
class Resampler {
  float history[8][128];
  uint32_t inputIndex=0, nextFifths=96*5;
  bool primed=false;
  uint8_t lastPhase=0;
public:
  unsigned phase() const { return lastPhase; }
  void reset() { memset(history,0,sizeof(history)); inputIndex=0; nextFifths=96*5; primed=false; }
  bool push(const int32_t *input, int32_t *output) {
    const uint32_t n=inputIndex++;
    for (int ch=0;ch<8;++ch) history[ch][n&127]=float(input[ch]);
    if (!primed && n<96) return false;
    primed=true;
    if (n<nextFifths/5) {
      if (inputIndex>=1048576) { inputIndex-=1048576; nextFifths-=5242880; }
      return false;
    }
    const unsigned phase=nextFifths%5;
    lastPhase=phase;
    for (int ch=0;ch<8;++ch) {
      float total=0;
      for (unsigned tap=0;tap<96;++tap)
        total += history[ch][(n-tap)&127]*FILTER[phase][tap];
      // The FIR can overshoot on a transient: saturate, never wrap 24 bits.
      output[ch]=int32_t(lroundf(fmaxf(-8388608.0f,fminf(8388607.0f,total))));
    }
    nextFifths+=8;
    // Keep the time accumulator small; subtract an exact 5/8 cycle.
    if (inputIndex>=1048576) { inputIndex-=1048576; nextFifths-=5242880; }
    return true;
  }
};
