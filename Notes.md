LCD
https://www.lcdwiki.com/3.2inch_RPi_Display
RPI
https://cdn.sparkfun.com/assets/learn_tutorials/1/5/9/5/GPIO.png


Lets create a plan to create a robot programed in python to create a robot in my Raspberry Pi, here is my Stack:

"Brain" Computer, Raspberry Pi 4 Model B, 4 GB, Raspberry Pi OS 64-bit, SD 64 GB.
"Legs" Traccion Chasis Tamiya 70108 + gearbox de 2 motors FA-130: 3V, range 1.5–3V, motor driver SparkFun TB6612FNG (dual) 
"Eyes" Camera OV5647 con módulo IR Stack libcamera / Picamera2
"Face" Screen 3.2inch_RPi_Display SPI
"Arms"  2 × servo, PWM 50 Hz vía pigpio, a 4.8V 
"Voice"  Audio Jack 3.5 mm de la Pi
"Sensors" 4 × vl53l0/1xv2
Batery, lipo 2s 6000mah.

the idea is that my robot can move with the Chasis Tamiya 70108 like a tank, and move the arms, all this controlled by a web interface through wifi. 
Wally must have a face displayed in the screen, and voice to make sounds by the audio output, though the camera must be able to detect objects like persons and animals like cat and dogs.
also must have a routine like follow the cat, the sensors will help to Wally to not crash with objects.