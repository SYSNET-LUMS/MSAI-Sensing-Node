#include "../main/deadline_recovery.h"

constexpr bool observed_short_gap() {
  DeadlineRecovery recovery;
  return recovery.allow(2, 289, 778899000, 16000);
}
constexpr bool repeated_gap_stops() {
  DeadlineRecovery recovery;
  return recovery.allow(2, 289, 1000000, 16000) &&
         !recovery.allow(2, 289, 60999999, 16000) &&
         recovery.allow(2, 289, 61000000, 16000);
}
constexpr bool severe_gaps_stop() {
  DeadlineRecovery recovery;
  return !recovery.allow(33, 1169, 1000000, 16000) &&
         !recovery.allow(5, 300, 1000000, 16000) &&
         !recovery.allow(2, 501, 1000000, 16000) &&
         !recovery.allow(2, -1, 1000000, 16000) &&
         !recovery.allow(1, 100, 1000000, 16000) &&
         !recovery.allow(2, 289, 1000000, 0) &&
         recovery.allow(4, 500, 1000000, 16000);
}
constexpr bool slow_rates_and_first_event() {
  DeadlineRecovery two_k, one_k, boot;
  return two_k.allow(2, 1200, 1000000, 2000) &&
         one_k.allow(2, 2400, 1000000, 1000) &&
         boot.allow(2, 289, 0, 16000) &&
         !boot.allow(2, 289, 1, 16000);
}
static_assert(observed_short_gap(), "Observed isolated 289 us gap must recover");
static_assert(repeated_gap_stops(), "Recovery needs a full minute between gaps");
static_assert(severe_gaps_stop(), "Severe gaps and invalid input must fail closed");
static_assert(slow_rates_and_first_event(), "Timing limits use the native ADC rate");
