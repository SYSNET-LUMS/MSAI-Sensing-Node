#pragma once
#include <stdint.h>
// 4 = R4 with CT selectors/relays (default).
// 1 = original A1 WITHOUT selectors/relays. This profile requires a fitted
// 20-ohm, 1%, >=0.25 W resistor across EACH USED CT secondary/J1 pair.
// Do not select profile 1 without physically installing those burdens.
#ifndef MCSA_BOARD_PROFILE
#define MCSA_BOARD_PROFILE 4
#endif
static_assert(MCSA_BOARD_PROFILE==1 || MCSA_BOARD_PROFILE==4,"Choose PCB profile 1 or 4");
namespace Board {
constexpr bool hasSelectors=MCSA_BOARD_PROFILE==4;
constexpr const char *revision=hasSelectors?"R4":"A1";
constexpr double externalBurdenOhms=hasSelectors?0.0:20.0;
constexpr int drdy=9, cs=10, mosi=11, sck=12, miso=13, reset=14;
constexpr int type=8, range=21, coil=47, analogFault=2;
constexpr int feedback[4]={38,39,40,41};
constexpr int statusLed=17, faultLed=18, ethCs=7, ethReset=15;
constexpr uint32_t clockHz=8192000, adcRate=16000, outputRate=10000;
constexpr uint32_t spiHz=12000000, baud=3000000;
// Stock R4 CR input: released relays connect the four parallel 160-ohm
// burdens (40 ohms). Includes nominal fuse/contact and attenuator loading.
// 3100:1 is the manufacturer's typical effective ratio, not a calibration.
constexpr double effectiveRatio=3100.0;
constexpr double a1AdcLoad=1.0/(1.0/44000.0+1.0/330000.0);
constexpr double a1Series=2.0*(3320.0+17.0+200.0);
constexpr double a1VoltsPerSecondaryAmp=20.0*a1AdcLoad/(20.0+a1Series+a1AdcLoad);
constexpr double adcVoltsPerSecondaryAmp=hasSelectors?3500.0/315.51409971218:a1VoltsPerSecondaryAmp;
constexpr double nominalAmpsPerAdcMv=effectiveRatio/adcVoltsPerSecondaryAmp/1000.0;
constexpr double mvPerCount=1200.0/8388608.0;
}
