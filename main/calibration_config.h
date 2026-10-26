#pragma once
#include "board_config.h"
namespace Calibration {
// Exact factors from the user's successful R4 CR/75 A Serial Monitor test.
// All four CTs measured the same 1.56 A clamp-meter reference. Mean AC RMS
// counts from captures 2279-2282; retain each CT on its calibrated channel.
// This is a one-point reference adjustment, not full-range characterization.
constexpr double ctReferenceAmps=1.56;
constexpr double ctReferenceRmsCounts[4]={
  (41985.2+41927.4+41902.6+41931.6)/4.0,
  (41897.0+41848.4+41814.6+41850.6)/4.0,
  (41906.9+41855.1+41827.4+41860.1)/4.0,
  (41824.7+41770.0+41743.5+41811.8)/4.0
};
constexpr double ctAmpsPerMv[4]={
  Board::hasSelectors?ctReferenceAmps/(ctReferenceRmsCounts[0]*Board::mvPerCount):Board::nominalAmpsPerAdcMv,
  Board::hasSelectors?ctReferenceAmps/(ctReferenceRmsCounts[1]*Board::mvPerCount):Board::nominalAmpsPerAdcMv,
  Board::hasSelectors?ctReferenceAmps/(ctReferenceRmsCounts[2]*Board::mvPerCount):Board::nominalAmpsPerAdcMv,
  Board::hasSelectors?ctReferenceAmps/(ctReferenceRmsCounts[3]*Board::mvPerCount):Board::nominalAmpsPerAdcMv
};
// CAL4 cs is A per RAW ADC count, whereas ct is A per ADC mV.
// The portal applies these slopes to its AC RMS; do not scale binary samples.
constexpr double ctSlopes[4]={
  ctAmpsPerMv[0]*Board::mvPerCount,
  ctAmpsPerMv[1]*Board::mvPerCount,
  ctAmpsPerMv[2]*Board::mvPerCount,
  ctAmpsPerMv[3]*Board::mvPerCount
};
constexpr double ctIntercepts[4]={0,0,0,0}; // Amperes; applied to AC RMS only.
// R4 channels are reference-adjusted. Do not transfer these factors to A1.
constexpr bool ctCalibrated[4]={Board::hasSelectors,Board::hasSelectors,Board::hasSelectors,Board::hasSelectors};
constexpr unsigned rmsSamples=2000; // Portal Welford RMS: 200 ms at 10 kSPS.
constexpr double countsPerMv=8388608.0/1200.0;
constexpr double voltageIntercepts[3]={0,0,0};
constexpr double voltageZeroThreshold=0;
// IsoBlock V-4c +/-1000 V -> +/-10 V DIFFERENTIAL: factory ratio 100:1.
// Native A1/R4 netlists: each leg has 49.9k series, 2k shunt, ADG4612,
// 200 ohms, then 22k bleed. The gain-1 ADC is modeled as 330k differential.
// 20-ohm differential sensor output impedance is split into 10 ohms/leg.
// Use 5-ohm nominal switch Ron; variation and component/reference tolerance
// are reasons to CHECK readings against a meter, even with factory scaling.
constexpr double isoBlockRatio=1000.0/10.0;
constexpr double voltageAdcLoadPerLeg=1.0/(1.0/22000.0+2.0/330000.0);
constexpr double voltageFilterSeries=200.0+5.0;
constexpr double voltageLoadedShunt=1.0/(1.0/2000.0+1.0/(voltageFilterSeries+voltageAdcLoadPerLeg));
constexpr double voltageAdcGain=voltageLoadedShunt/(49900.0+10.0+voltageLoadedShunt)*
                               voltageAdcLoadPerLeg/(voltageFilterSeries+voltageAdcLoadPerLeg);
constexpr double nominalVoltsPerAdcMv=isoBlockRatio/voltageAdcGain/1000.0;
constexpr double voltageNominalSlopes[3]={
  nominalVoltsPerAdcMv*Board::mvPerCount,
  nominalVoltsPerAdcMv*Board::mvPerCount,
  nominalVoltsPerAdcMv*Board::mvPerCount
};
// CAL4 vv means the portal may use the voltage scale. Nominal factory/PCB
// scaling is valid for starting measurements, NOT proof of bench calibration.
constexpr bool voltageNominalEnabled=true;
// A6 reference calibration can override this scale in an IsoBlock-specific
// NVS namespace, keeping factors from other sensor assemblies separate.
}
