#pragma once
#include <stdint.h>

// Clear once after a continuous ten-second hold. Debounce release before reboot
// so holding BOOT cannot cause repeated resets or an ambiguous LED indication.
class ResetButton {
public:
  enum class Action { None, Clear, Restart };
  Action update(bool pressed,uint32_t now) {
    if(waiting_release){
      if(pressed){releasing=false;return Action::None;}
      if(!releasing){releasing=true;released_at=now;}
      if(uint32_t(now-released_at)>=60){
        waiting_release=false;holding=false;releasing=false;
        if(success)return Action::Restart;
      }
      return Action::None;
    }
    if(!pressed){holding=false;return Action::None;}
    if(!holding){holding=true;started_at=now;}
    if(uint32_t(now-started_at)>=10000){waiting_release=true;return Action::Clear;}
    return Action::None;
  }
  void complete(bool cleared){success=cleared;}
private:
  bool holding=false,waiting_release=false,releasing=false,success=false;
  uint32_t started_at=0,released_at=0;
};
