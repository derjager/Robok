# Plan: Wally, robot tanque con cara, ojos y brazos

Borrador v2 — 2026-09-21. Lo marcado **[VERIFICADO]** se comprobó en la Pi; lo marcado **[SUPUESTO]** hay que confirmarlo.
Las decisiones abiertas están en la sección 7. Pines y cableado: [PINOUT.md](PINOUT.md) y el [diagrama](docs/wiring.svg).

## 1. Objetivo

Robot sobre chasis Tamiya 70108 (orugas), controlado por WiFi desde una interfaz web (idealmente el celular), con:

- **Cara** en el LCD que expresa el estado del robot.
- **Voz**: sonidos y habla por el jack de audio.
- **Ojos**: cámara que detecta personas, gatos y perros.
- **Brazos**: 2 servos.
- **Rutina autónoma** "seguir al gato", con los sensores ToF evitando choques.

## 2. Estado actual de la Pi [VERIFICADO]

| Área | Estado |
|---|---|
| Sistema | Pi 4 (4 GB), Debian 13 Trixie, Python 3.13.5, kernel 6.18, 4 núcleos, 49 GB libres |
| Red | WiFi 5 GHz conectado (10.0.0.222), hostname `Wally`, ssh y avahi activos (`wally.local`) |
| LCD | **OK y orientado.** `/dev/fb1`, framebuffer **vertical 240×320** (RGB565, SPI a 16 MHz) con `dtoverlay=tft9341:rotate=0`, aplicado y confirmado visualmente. Pruebas: [lcd_test.py](scripts/lcd_test.py) y [lcd_orient.py](scripts/lcd_orient.py) |
| Touch del LCD | `ads7846` cargado (`/dev/input/event0`). No es un requisito; el overlay propio `wally-lcd` lo elimina |
| Cámara | OV5647 detectada por libcamera (640x480@62fps, 1296x972@46fps, 1920x1080@33fps) |
| Audio | **OK.** Tarjeta `Headphones` (jack) como salida por defecto, vía `~/.asoundrc` (hecho con raspi-config). Volumen `PCM` al 96 %. Tono de prueba estéreo escuchado |
| Voz | `espeak-ng` 1.52 instalado. Voz `es-419` probada con tres tonos y velocidades, y escuchada sin problemas |
| Térmico | 53–55 °C en reposo, sin throttling (`get_throttled=0x0`) |
| Paquetes | En apt: picamera2, opencv, fastapi, uvicorn, numpy, pillow, gpiozero, lgpio, smbus2, espeak-ng |
| Entorno del proyecto | `.venv` con `--system-site-packages` y las dependencias instaladas (pillow, numpy, fastapi, uvicorn, websockets, pytest). más de 160 tests pasan. Ver [README.md](README.md) |
| **Falta** | **`pigpiod` no existe en Trixie**. Solo está el cliente `python3-pigpio`. `picamera2` está en apt pero sin instalar |

Con `hdmi_group/hdmi_mode/hdmi_cvt` (480x360) y `vc4-kms-v3d` comentado, la Pi usa framebuffer legacy. Picamera2 sin ventana de preview funciona igual.

> **Advertencia:** `LCD32-show` **sobrescribe `config.txt`** desde una copia. Si se vuelve a ejecutar, se pierden los overlays que agreguemos después. Antes de tocar `config.txt` hay que hacer un respaldo, y no se debe volver a correr `LCDxx-show`. El respaldo que ya existe (`config.txt.bak-lcdshow`) tiene la configuración original con `rotate=270`, que es la orientación equivocada para este montaje.

## 3. Presupuesto de pines

Con el LCD conectado **por cables** (8 hilos, sin touch), el header deja de ser un problema. Mapa completo, lista de cables y procedimiento en [PINOUT.md](PINOUT.md). Diagrama pin a pin: [docs/wiring.svg](docs/wiring.svg).

| Bloque | GPIO | Cant. |
|---|---|---|
| LCD (SPI0 + DC + RST) | 8, 10, 11, 22, 27 | 5 |
| I2C1 hardware (4×ToF y luego ADS1115, IMU…) | 2, 3 | 2 |
| Motores TB6612 (PWMA, AIN1, AIN2, PWMB, BIN1, BIN2, STBY) | 16, 19, 20, 21, 23, 24, 26 | 7 |
| Servos | 12, 13 (PWM por hardware) | 2 |
| XSHUT de los 4 ToF (nacen todos en 0x29) | 4, 5, 6, 25 | 4 |
| **Usados** | | **20** |
| **Libres** | 18 hoy; +7, 9, 17 tras los overlays; +14, 15 si se apaga la consola serie | **hasta 6** |

Consecuencias frente a la versión anterior de este plan:

- **Ya no hace falta PCA9685 ni I2C por software.** Los ToF van por el I2C hardware (GPIO2/3).
- Los sensores nuevos que vayan por I2C no consumen pines.
- Liberar el touch requiere un overlay propio del LCD (escrito y validado en seco: [system/wally-lcd-overlay.dts](system/wally-lcd-overlay.dts)) más `spi0-1cs,no_miso`.
- Los pines de seguridad de motores (todos GPIO 9–27) arrancan en pull-down, así que el robot no se mueve durante el arranque.

## 4. Energía

La LiPo 2S es de 7.4 V nominal (8.4 V llena, no bajar de 6.0 V). Lo que entrega hoy no sirve directo para nada de lo que hay a bordo:

| Consumidor | Alimentación propuesta |
|---|---|
| Raspberry Pi 4 (+cámara, WiFi, LCD) | **BEC 1**: 5.1 V ≥ 3 A **dedicado**, por el pin 4 del header |
| Servos | **BEC 2**: 4.8–5 V ≥ 3 A **separado** (los servos en stall no deben tumbar a la Pi) |
| Motores FA-130 (VM del TB6612) | **Buck** ~3–3.3 V ≥ 2 A. Alternativa sin buck: limitar el duty a ≤ 40 % en software |
| Lógica TB6612, ToF y lógica del LCD | 3.3 V de la Pi |

Además: masa común entre todo, interruptor y fusible en BAT+, y una alarma de batería baja. El monitoreo de voltaje necesita un ADC (ADS1115 por I2C), que es opcional y va en la fase final. Detalles y advertencias (alimentar la Pi por el pin 4, un solo BEC) en [PINOUT.md](PINOUT.md) §6.

## 5. Arquitectura de software

Un solo proceso Python, con asyncio (FastAPI) para la web y hilos para lo bloqueante (cámara, detector, ToF).

```
robok/
  config.py / config.toml    pines, límites, calibración
  hal/                       una clase por hardware, cada una con versión Fake
    display.py  motors.py  servos.py  tof.py  camera.py  audio.py  battery.py
  services/
    face.py        expresiones y animación
    voice.py       sonidos y habla
    vision.py      detector + tracking
    safety.py      watchdog, límites, paro de emergencia
  behaviors/       manual, idle, follow_target, search, avoid
  web/             FastAPI + UI estática (HTML/JS sin build)
  main.py
scripts/           pruebas de bring-up por subsistema (lcd_test, lcd_orient, face_demo)
docs/              diagrama de cableado y su generador
system/            overlays de device tree
tests/             pytest sobre lo crítico: safety y comportamientos
systemd/wally.service
```

**Decisiones de diseño:**

- **HAL con versión Fake** (`ROBOK_SIM=1`): permite desarrollar y probar la lógica sin robot, y en el escritorio.
- **Entorno**: venv con `--system-site-packages` para reutilizar picamera2/opencv/lgpio de apt. Con pip solo lo que apt no tenga.
- **Arbitraje de control** a 20 Hz: `safety` > `manual` > `autónomo`. Cualquier comando de motores pasa por `safety`.
- **Cara**: se dibuja con Pillow y se escribe directo a `/dev/fb1` (sin X, sin fbcp).
  - **Se diseña para pantalla vertical 240×320.** El código lee el tamaño desde sysfs, no lo fija.
  - El límite físico son ~13 fps a pantalla completa (calculado: 1.23 Mbit / 16 MHz ≈ 77 ms por cuadro).
  - Para animaciones más rápidas hay que actualizar solo la zona de los ojos, o probar un SPI más rápido (`speed=32000000`).
- **Web**:
  - FastAPI + WebSocket JSON a ~20 Hz para control y telemetría.
  - Video MJPEG en un `<img>`.
  - Joystick táctil que mezcla a tanque, sliders de brazos, selector de expresión, botones de sonido, selector de modo, lecturas de sensores y botón de paro.
  - Diseño pensado primero para el celular.
  - Acceso solo por LAN con PIN/token simple, porque mueve un robot físico.
- **Cámara**: Picamera2 con dos streams. Uno `main` de 640x480 para la web y otro `lores` de 320x240 para el detector.
- **Visión**: CPU solamente (sin acelerador). Clases COCO: persona, gato, perro.
  - Candidatos a comparar: YOLO nano en NCNN, EfficientDet-Lite/TFLite, OpenCV DNN.
  - La elección se hace **midiendo fps reales en esta Pi**, con una meta de ≥ 8 fps a ~320 px. No se decide por lectura.
- **Voz**:
  - Habla: `espeak-ng` con la voz `es-419`, que ya funciona. El tono (`-p`) y la velocidad (`-s`) se cambian por frase: grave y lento al iniciar, agudo y rápido al encontrar al gato.
  - Efectos: WAV generados con numpy.
  - Opcional después: `piper-tts` (pip; ojo, el paquete apt `piper` es otra cosa) para voz natural.

### Reglas de seguridad (van en `safety`, con tests)

1. **Watchdog**: sin latido del cliente web en 500 ms, los motores se paran.
2. **Obstáculos**:
   - ToF frontal < ~35 cm: velocidad limitada.
   - < ~20 cm: se bloquea el avance.
   - Cerca de un obstáculo trasero: se bloquea la reversa.
3. Tope de duty de motores según su voltaje nominal, y rampa de aceleración (evita picos de corriente y brownouts).
4. Servos con límites mecánicos y movimiento suave. Sin pulso cuando están inactivos.
5. Batería: aviso en ~6.6 V, motores off en ~6.2 V, apagado seguro en ~6.0 V.
6. Temperatura: bajar la tasa del detector por encima de ~75 °C.
7. Al salir el proceso, o por systemd: STBY a bajo y salidas en estado seguro.

### Seguir al gato

- **Giro**: controlador proporcional sobre el error horizontal del centro del bounding box.
- **Distancia**: el tamaño del box y el ToF frontal dicen cuánto acercarse. El ToF tiene prioridad.
- **Parada**: se detiene a ~30 cm.
- **Pérdida del objetivo**: si se pierde más de N segundos, gira lento buscando. Si no lo encuentra, vuelve a reposo.
- **Cara**: emoción según el estado (buscando, encontrado, obstáculo, batería baja).

## 6. Fases

Tamaños relativos: S ≈ una sesión, M ≈ unas pocas, L ≈ varias. Cada fase termina con un script de bring-up y una prueba física.

| # | Fase | Tam. | Resultado verificable | Estado |
|---|---|---|---|---|
| 0 | **Fundaciones**: estructura del repo, `git init`, venv, config.toml, logging, overlays del LCD (Etapa A de PINOUT.md §8), recablear el LCD (Etapa B), probar que pigpio compila | S | El LCD funciona por cables con GPIO 7/9/17 libres; el proyecto arranca en modo simulado; se sabe si pigpio compila | **En curso**: estructura, venv, `config.toml`, logging, tests y unidad systemd (sin instalar) ✔; audio y LCD orientado ✔. Falta: overlays del LCD (Etapa A/B), probar pigpio y el primer commit |
| 1 | **Cara + esqueleto web**: servicio de cara con expresiones (vertical 240×320), app FastAPI, UI base, health | M | Cambiar la expresión de Wally desde el celular | **Hecha**: 8 expresiones con parpadeo y mirada en el LCD real, y web (API, WebSocket, UI para celular). Verificada leyendo `/dev/fb1` de vuelta y probada por ti |
| 2 | **Voz y sonidos** (independiente, se puede adelantar) | S | Botón que hace sonar a Wally y una frase con espeak-ng | **Parcial**: `voice.py` listo (voz por estado de ánimo y 5 efectos) y accesible desde la web. Falta enlazarlo a eventos del robot |
| 3 | **Motores**: TB6612 + BEC/buck, PWM (pigpio o lgpio), mezcla a tanque, joystick, watchdog, tope de duty | M | Manejarlo desde el celular; el watchdog lo frena al cortar el WiFi | **Software listo, sin hardware probado** (ver abajo) |
| 4 | **ToF por I2C1**: secuencia de XSHUT y direcciones, filtro, limitador de obstáculos, telemetría | M | Lecturas estables de los 4 sensores; no avanza contra una pared | **Software listo, sin hardware probado** (ver §9). Falta cablear los sensores |
| 5 | **Brazos**: servos en GPIO12/13, límites, presets (saludar), sliders | S | Saluda desde la UI sin jitter | Pendiente |
| 6 | **Cámara + stream**: MJPEG en la UI | S | Video en el celular; medir latencia y CPU | **Hecha y probada con la cámara real** (ver §9). Falta verlo en tu celular |
| 7 | **Detección**: benchmark de 2–3 backends (spike), filtro persona/gato/perro, tracking, overlay opcional | L | fps medidos; detecta al gato de forma estable | **Software listo y probado con modelos reales sobre fotos** (ver §10). Falta probarla con Pila en persona |
| 8 | **Comportamientos**: FSM (reposo / seguir / buscar / evitar), afinado del P, reacciones de la cara | L | Sigue al gato sin chocar, con paro por UI y por ToF | Pendiente |
| 9 | **Robustez**: servicio systemd, monitor de batería (ADS1115), térmico, logs, README | M | Arranca solo al encender y se recupera de fallos | Pendiente |

**Riesgos a probar en su fase, no a suponer:**

- **Audio y PWM**: solo aplica si se usa PWM por hardware del kernel (opción B de PINOUT.md §9). El jack (PWM1 del SoC) y GPIO12/13 podrían interferirse por reloj compartido. Se prueba en la fase 3 reproduciendo audio con los servos activos. pigpio (opción A) usa el reloj PCM y no tiene ese problema.
- **Motores reales sin probar**: toda la lógica está probada con GPIO falso, pero el PWM por software de `lgpio` (1 kHz) puede tener jitter con la CPU cargada. Es tolerable para motores; si molesta, pigpio compilado o PWM por hardware. Se mide en la puesta en marcha (`scripts/motor_test.py`).
- **Ruido del jack**: el jack de la Pi 4 suele meter siseo. Si molesta, un mini amplificador (PAM8403) con parlante lo mejora. En la prueba de voz no hubo quejas.
- **LCD por cables**: el SPI a 16 MHz por cables sueltos puede dar artefactos. Cables cortos con GND al lado; si falla, bajar `speed`.
- **ToF**: no ve bien superficies negras o transparentes, y el sol interfiere. Además el cono es angosto (~25°), así que quedan puntos ciegos.
- **CPU**: detector + video + cara + web en 4 núcleos. Conviene disipador o ventilador.
- **IR**: con el módulo IR la imagen puede ser en escala de grises y bajar la precisión del detector.
- **Un solo BEC**: si el 5 V de la Pi y de los servos sale del mismo BEC, los picos de los servos pueden reiniciar la Pi.

## 7. Decisiones

**Ya tomadas:**

- LCD en **vertical 240×320** (`rotate=0`), según el montaje físico.
- Audio por el **jack**, con `espeak-ng` en español latinoamericano (`es-419`) para la voz.
- El LCD va por cables, sin touch; se libera GPIO 7, 9 y 17.
- ToF: **VL53L0X** ×4, a 3.3 V. Cámara en la web: MJPEG por `rpicam-vid` (sin picamera2).

**Abiertas (con mi recomendación):**

1. **PWM de servos y motores.** `pigpiod` no existe en Trixie. Ya no faltan pines, así que el PCA9685 dejó de ser necesario. → *Recomiendo probar pigpio compilado desde fuente (tu stack original) en la Fase 0, con lgpio + PWM hardware como plan B.* Detalle en PINOUT.md §9.
2. ~~**Sensores ToF.**~~ **Resuelta:** son VL53L0X (confirmado con foto del módulo; alcance útil ~3–120 cm). Posiciones sugeridas: frente-centro, frente-izquierda, frente-derecha y atrás. Se alimentan a 3.3 V.
3. **Energía.** ¿Cuántos BEC tienes y de qué corriente? ¿Ya tienes el buck de 3 V para los motores? → *Dos BEC (Pi y servos) y buck de 3 V si puedes.*
4. **Voz.** ¿Qué tono te gustó de las tres frases de prueba (normal, grave o aguda)? ¿Voz robótica de espeak-ng o natural con piper? → *Empezar con espeak-ng.*
5. **Control.** ¿Solo desde el navegador del celular, o también teclado/gamepad en PC? → *Celular primero.*
6. **Micrófono.** Salió de tus notas. Lo asumo fuera del alcance. Si vuelve, el MAX4466 es analógico y la Pi no tiene ADC de audio, así que haría falta una tarjeta de sonido USB.
7. **Otros sensores.** ¿Cuáles y de qué tipo (I2C, digital, analógico)? Los I2C no consumen pines; los digitales sí (hoy sobran hasta 6). Los analógicos necesitan un ADC (ADS1115 por I2C).

## 8. Fase 3 (motores): qué hay y qué falta

**Hecho y probado sin hardware** (más de 160 tests, incluida una prueba de mutación de las protecciones):

- Driver del TB6612 ([robok/hal/motors.py](robok/hal/motors.py)): tabla de verdad, STBY primero al parar y último al arrancar, PWM por software con `lgpio`, estado seguro al iniciar y cerrar, y corrección de sentido/lados por configuración.
- Seguridad ([robok/services/safety.py](robok/services/safety.py)) y control ([robok/services/drive.py](robok/services/drive.py)): mezcla de tanque con zona muerta, tope de potencia, rampa, watchdog, paro de emergencia con bloqueo, límite por obstáculos (a la espera de los ToF), y **fail-safe**: si el bucle de control falla, paro de emergencia.
- API y web: manejo por REST y WebSocket, joystick táctil (y WASD en PC), control de velocidad, barras de potencia, botón de PARO siempre visible, y parada al desconectarse el cliente, ocultar la página o perder el foco.
- La cara y la voz reaccionan: enojada con sonido de error al paro; sorprendida ante un obstáculo; pensativa al saltar el watchdog.
- Activación segura: `motors.enabled = false` por defecto; si falla el GPIO cae a motores simulados y lo dice.

**Falta, y necesita hardware o tu decisión:**

1. Cablear el TB6612 y la alimentación (BEC y buck de 3 V, o decidir el tope de potencia si VM = batería).
2. Correr `scripts/motor_test.py --go` con las orugas en el aire y copiar el resultado a `config.toml`.
3. Probar el joystick real y medir el jitter del PWM por software bajo carga (y, si molesta, compilar pigpio).
4. Probar que el audio del jack no interfiera con el PWM (riesgo anotado en la sección 6).

## 9. Fase 4 (ToF) y cámara en la web: qué hay y qué falta

**Cámara (probada con la OV5647 real):**

- [robok/hal/camera.py](robok/hal/camera.py): lanza `rpicam-vid --codec mjpeg` y parte su salida en fotogramas JPEG (el separador recorre los segmentos del JPEG, no busca bytes a ciegas). Se **enciende sola con el primer espectador y se apaga `idle_s` segundos después del último**, así no gasta CPU ni calienta la Pi cuando nadie mira. Si el proceso muere se reinicia con espera creciente, y si queda vivo pero mudo (5 s sin imágenes) también.
- Web: `GET /api/camera.mjpg` (video para un `<img>`), `GET /api/camera.jpg` (foto) y una tarjeta en la UI con las distancias de los ToF superpuestas. Ambos aceptan `?token=` porque una etiqueta `<img>` no puede mandar cabeceras.
- Medido: 640×480 a ~15 fps, ~14 KB por fotograma (~210 KB/s por espectador), ~11 % de un núcleo. El detector de la fase 7 podrá usar estos mismos fotogramas.

**ToF (probado solo con simuladores):**

- Driver [robok/hal/vl53l0x.py](robok/hal/vl53l0x.py): port de la librería de Adafruit (MIT) sin CircuitPython. Se verificó contra el original ejecutando ambos sobre el mismo sensor simulado: las 137 escrituras (145 registros) y las 38 lecturas de la inicialización son idénticas, con cuatro mapas SPAD distintos; un test guarda el hash de esa secuencia.
- Arreglo [robok/hal/tof.py](robok/hal/tof.py): apaga los 4 XSHUT, enciende y direcciona uno por uno (0x30–0x33), lee a 20 Hz con mediana de 3, y reinicia **solo** al sensor que se cuelga. Si el I2C o el GPIO no abren, todo queda en error: nunca se sustituye por sensores simulados.
- Servicio [robok/services/obstacles.py](robok/services/obstacles.py) y `Drive`: frente = el más cercano de los tres frontales; atrás = el trasero. **Fail-safe:** un sensor listado que falla, o que aún no da su primera lectura, cuenta como obstáculo a 0 cm en su dirección; y si dejan de llegar datos al `Drive` (0,5 s), se bloquean avance y reversa. Con ToF activados el robot nace bloqueado hasta la primera lectura. El giro nunca se bloquea.
- UI: distancias sobre el video, en verde/ámbar/rojo con los mismos umbrales del limitador; `falla` en rojo. La cara se pone triste y suena el error cuando un sensor falla.
- `tof.enabled = false` por defecto: con el bus vacío, activarlo bloquearía el avance.

**Falta, y necesita hardware o tu decisión:**

1. Cablear los 4 VL53L0X (VIN 3.3 V, GND, SDA, SCL, XSHUT; `GPIO1` sin conectar) y correr `scripts/tof_test.py`.
2. Poner `tof.enabled = true` (y en `tof.sensors` solo los que hayas instalado) y comprobar que el robot no avanza contra una pared.
3. Ver la tarjeta de cámara en tu celular: la interfaz nueva no se ha probado en tu navegador.
4. En las dos fotos de prueba un objeto rosado y luminoso, muy cerca del lente, tapa casi todo el encuadre (¿la pantalla LCD, la cinta o un cable?); el resto de la habitación sale con colores normales, así que el sensor está bien. Conviene apartarlo o reorientar la cámara. Si la imagen sale al revés o espejada, usa `camera.hflip` / `camera.vflip`.

## 10. Fase 7 (visión): personas, gatos, quién es quién y mirada

**Decisiones (medidas en esta Pi, no supuestas):**

- **Detector:** EfficientDet-Lite0 int8 en TFLite (`ai-edge-litert`), con NMS incluido: 94 ms con 3 hilos, 138 ms con 2 (los que usa por defecto); la variante Lite2 tardó 260 ms y se descartó. El modelo de MediaPipe sin NMS obligaba a decodificar 19 206 anclas a mano y también se descartó. Se filtra la caja "contenedora" que a veces añade alrededor de dos gatos.
- **Gatos:** huella MobileNetV3-small (1024 números, 18 ms) y comparación por coseno contra las muestras enseñadas (promedio de las 3 mejores, umbral 0.65 y margen 0.05 sobre la segunda identidad). En fotos reales: mismo gato ≥ 0.81, gato distinto ≤ 0.60. La variante *large* fue peor y se descartó.
- **Personas:** por la cara, YuNet + SFace (~210 ms por cara): misma persona ≥ 0.84 y personas distintas 0.01 (umbral 0.40). Sin cara visible no se identifica, pero la pista conserva el nombre 8 s.
- **Identidad estable:** cada objeto seguido (tracker por IoU) acumula votos y la identidad se decide por votos con histéresis, no por un solo cuadro.
- **Enseñar:** sesión que toma muestras de UN solo sujeto (con dos gatos espera), una cada ~0,7 s, descartando las casi idénticas; guarda huella y miniatura en `data/vision/`.
- **Mirada:** los ojos siguen a las identidades marcadas con "seguir"; espejo por defecto; al perderlo 1,5 s vuelven al centro.
- **Costo:** con 4 análisis/s, ~90 % de un núcleo de 4 más ~11 % de la cámara; 61 °C. Baja a la mitad por encima de 75 °C.
- **Modelos:** `scripts/get_models.py` los descarga (48 MB) y verifica su SHA-256; van en `models/`, ignorados por git.

**Verificado:** pipeline completo con los modelos reales sobre fotos (dos gatos y dos personas reconocidos correctamente, y la mirada apunta al gato marcado); interfaz completa en Chromium con una escena simulada (enseñar, reconocer, seguir, apagar, borrar); 427 pruebas y prueba de mutación de la lógica nueva.

**Falta, y necesita a Pila en persona:**

1. Enseñarle a Pila desde la web y ver si la reconoce en tu casa. Las pruebas con fotos usaron muestras de la misma imagen (optimista): con luz y ángulos distintos puede hacer falta más muestras, o ajustar `cat_threshold`. `scripts/vision_test.py --live 30` muestra las puntuaciones.
2. Enseñar también a los otros gatos de la casa, para que compare entre ellos.
3. Decidir si la mirada en espejo (por defecto) es la que quieres o prefieres la contraria (casilla en la tarjeta).
4. Con gatos casi gemelos, un modelo genérico puede confundirlos: el siguiente paso sería un embedder más fino (p. ej. DINOv2-small), más lento.
5. Reiniciar tu instancia de Wally para que cargue el código nuevo.
