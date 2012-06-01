
#include "pwm.h"

//The PWM has no loop function. It runs entirely out of the ISR.
void pwm_loop(){}

#ifdef HAS_PWM_SUPPORT

void pwm_setup(){
    //s/w "BCM"(<== "Binary Code Modulation") timer setup
    TCCR0A |= _BV(WGM00)|_BV(WGM01);
    TCCR0B |= _BV(CS02);
    TIMSK0 |= _BV(TOIE0);
    //FIXME set PWM output DDRs
}

//Software PWM stuff
//Called every 256 cpu clks (i.e should not get overly long)
ISR(TIMER0_OVF_vect){
    uint8_t Q = 0;
    Q |= (pwm_val[0] & pwm_cycle);
    Q |= (pwm_val[1] & pwm_cycle)?2:0;
    Q |= (pwm_val[2] & pwm_cycle)?4:0;
    Q |= (pwm_val[3] & pwm_cycle)?8:0;
    Q |= (pwm_val[4] & pwm_cycle)?16:0;
    PORTC = Q;
    OCR0A = pwm_cycle;
    pwm_cycle<<=1;
    if(!pwm_cycle){
        pwm_cycle = 1;
    }
}

uint8_t pwm_cycle = 0;
uint8_t pwm_val[PWM_COUNT];

#else//HAS_PWM_SUPPORT

void pwm_setup(){}

#endif//HAS_PWM_SUPPORT
