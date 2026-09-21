# Wally

Robot tanque con cara, voz, ojos y brazos, sobre una Raspberry Pi 4. Ver [PLAN.md](PLAN.md) (qué y por qué), [PINOUT.md](PINOUT.md) (pines y cableado) y el [diagrama](docs/wiring.svg).

## Estado

Hecho: la cara (8 expresiones en el LCD vertical 240×320), la voz (`espeak-ng` y efectos), una interfaz web para el celular, el **control de motores** con todas sus protecciones (joystick, watchdog, paro de emergencia, tope de potencia, rampas), la **vista de la cámara en la web** (probada con la OV5647 real) y los **sensores ToF VL53L0X** con su limitador de obstáculos (probados solo con simuladores). Los motores y los ToF reales están **desactivados por defecto** hasta cablearlos. La **visión** (detección de personas y gatos, reconocer quién es quién y seguir con la mirada a quien marques) está hecha y probada con modelos reales sobre fotos; falta probarla con Pila en persona. Los brazos vienen después.

## Uso

```bash
python3 -m venv --system-site-packages .venv      # una sola vez
.venv/bin/pip install -e ".[dev]"

.venv/bin/python -m robok                         # arranca Wally (LCD y audio reales)
.venv/bin/python -m robok --sim                   # sin hardware: sirve para desarrollar en cualquier equipo
.venv/bin/python scripts/face_demo.py             # recorre las expresiones en el LCD
.venv/bin/python scripts/motor_test.py            # plan de la puesta en marcha de motores (no toca pines)
.venv/bin/python scripts/tof_test.py              # enciende los ToF, asigna direcciones y muestra distancias en vivo
.venv/bin/python scripts/get_models.py            # descarga los modelos de la visión (~48 MB, una sola vez)
.venv/bin/python scripts/vision_test.py --live 30 # qué ve la cámara y a quién reconoce, con puntuaciones
```

Para el hardware real hacen falta los paquetes del sistema `python3-lgpio` y `python3-smbus2` (el venv usa `--system-site-packages`), y `rpicam-apps` para la cámara; en Raspberry Pi OS ya vienen.

Al arrancar imprime la dirección (`http://wally.local:8000`) y un **código de acceso**. Se guarda en `.robok_token` (permisos 600), o se fija con `ROBOK_TOKEN` o `web.token` en `config.toml`. La web es solo para la red local: el código evita que cualquiera en la WiFi mueva el robot, pero el tráfico no va cifrado.

## Motores: cómo activarlos

Sin `motors.enabled = true` en `config.toml` los motores son simulados (la interfaz lo avisa con una insignia). Para activarlos:

1. Cablea el TB6612 según [PINOUT.md](PINOUT.md) y alimenta VM (el buck de 3 V, o la batería con el tope de potencia por defecto de 40 %).
2. Con las **orugas en el aire**, ejecuta `.venv/bin/python scripts/motor_test.py --go`. Mueve un motor a la vez a baja potencia, te pregunta qué pasó y al final imprime las líneas `swap_sides`, `invert_left` e `invert_right` para `config.toml`.
3. Pon `enabled = true`, arranca `python -m robok` y prueba el joystick, todavía con las orugas en el aire.

Protecciones (siempre activas, con tests): paro de emergencia con bloqueo hasta reanudar; **watchdog** de 0,5 s (sin órdenes, los motores paran); si un cliente se desconecta, los motores paran; tope de potencia `max_duty`; rampa de aceleración; y un fallo en el bucle de control activa el paro de emergencia. Los límites por obstáculos se alimentan de los sensores ToF (ver más abajo).

## Cámara

La tarjeta **Cámara** de la interfaz muestra el video (MJPEG, ~15 fps a 640×480) con las distancias de los ToF encima. La cámara se enciende sola cuando alguien mira y se apaga unos segundos después de que se va; con el botón de la tarjeta se apaga a mano (por ejemplo, para ahorrar WiFi mientras manejas). Ajustes en `[camera]` de `config.toml` (`hflip`/`vflip` si la imagen sale girada). Para una foto suelta: `curl -H "X-Wally-Token: <código>" http://wally.local:8000/api/camera.jpg -o foto.jpg`.

## Visión: personas, gatos y quién es quién

**Una sola vez:** `.venv/bin/pip install -e ".[vision]"` y `.venv/bin/python scripts/get_models.py` (baja ~48 MB y verifica su SHA-256). Sin los modelos la visión queda apagada y la tarjeta lo dice; lo demás sigue igual.

**Qué hace.** La tarjeta **Visión** de la web dibuja recuadros sobre el video (amarillo = gato, azul = persona, violeta = perro) con el nombre y la puntuación de quien reconoce. Detecta con EfficientDet-Lite0; distingue **gatos** por su aspecto (huella MobileNetV3) y **personas por la cara** (YuNet + SFace, así que la persona debe mirar hacia la cámara; si se voltea, conserva su nombre unos segundos).

**Enseñarle a Pila** (o a una persona): en la tarjeta escribe el nombre, elige Gato o Persona, deja marcado "Seguirlo con la mirada" y pulsa **Enseñar**. Pon a *ese* gato solo frente a la cámara y deja que se mueva: toma 12 muestras (una cada ~0,7 s, y descarta las casi idénticas). Con dos gatos a la vez no sabe cuál es cuál, así que espera. Para reforzarla usa **Enseñar más** desde otros ángulos y con otra luz; tocando una miniatura se borra una muestra mala. **Conviene enseñar también a los demás gatos de la casa**: así compara entre ellos y no solo contra un umbral.

**Los ojos siguen a Pila.** Cuando el gato reconocido pertenece a una identidad con "seguir con la mirada", los ojos de la cara siguen su posición en el cuadro; al perderlo 1,5 s vuelven al centro. Por defecto la mirada es en **espejo**: los ojos miran hacia donde está el gato visto desde el frente del robot (gato a la derecha de la cámara, pupilas hacia la izquierda de la pantalla). Si al probar te parece al revés, desmarca "Mirada en espejo" en la tarjeta.

**Calibrar.** `.venv/bin/python scripts/vision_test.py --live 30` imprime, por cada gato o persona, el nombre aceptado o el mejor candidato con su puntuación aunque no llegue al umbral. Si confunde a dos gatos, sube `cat_threshold` (0.65) en `config.toml`; si no reconoce a Pila, bájalo o enséñale más muestras.

**Costo.** Con 4 análisis/s usa cerca de 1 núcleo de 4 (~90 %) más la cámara, y la Pi queda a ~61 °C; baja `vision.fps` para gastar menos. La tarjeta tiene un botón para apagarla y se recuerda. Se reduce sola a la mitad si la Pi pasa de 75 °C.

**Privacidad.** Las fotos y huellas (de gatos y de caras) se guardan **solo en el robot**, en `data/vision/` (fuera de git); las miniaturas son recortes de 96 px. Borrar una identidad borra todas sus muestras. Los modelos van en `models/`.

**Limitaciones honestas.** Distinguir a un gato de otro con un modelo genérico funciona bien con gatos de aspecto distinto y peor con gemelos (mismo color y patrón); las pruebas con fotos reales dieron 0,91–0,92 de similitud al gato correcto contra ≤0,60 al otro, pero con muestras de la misma foto, así que la vida real será más difícil. El detector puede perder gatos muy lejanos, tapados o de espaldas en poca luz.

## Sensores ToF: cómo activarlos

1. Cablea los VL53L0X según [PINOUT.md](PINOUT.md) §4.4 (VIN a 3.3 V; `GPIO1` sin conectar).
2. Ejecuta `.venv/bin/python scripts/tof_test.py` (o con `--sensors front` si solo cableaste uno). Pon la mano frente a cada sensor: debe cambiar solo su columna.
3. En `config.toml`: `[tof] enabled = true` y, si instalaste menos de 4, `sensors = [...]` con los que tengas.

Con los ToF activados el robot **nace bloqueado hasta la primera lectura**, un sensor listado que falla bloquea su dirección, y si dejan de llegar datos se bloquean avance y reversa. El giro nunca se bloquea. Con `--sim` los ToF son simulados: se les fija la distancia con `POST /api/sim/tof` (solo en simulación).

## Pruebas

```bash
.venv/bin/python -m pytest -q
```

Cubren configuración, conversión al formato del LCD, animación de la cara, voz (con un reproductor falso), el driver del TB6612 (con GPIO falso), toda la lógica de seguridad, la cámara (con un `rpicam-vid` falso), el driver del VL53L0X y el arreglo de sensores (con un sensor simulado) la visión (detector, galería de identidades, seguimiento, enseñanza y mirada, con una escena simulada) y la API completa, incluido el WebSocket. No necesitan hardware; las de los modelos reales se omiten si no están descargados.

## Estructura

| Carpeta | Contenido |
|---|---|
| `robok/hal/` | Una clase por hardware, cada una con su versión simulada |
| `robok/services/` | Cara, voz, control de movimiento, seguridad, obstáculos y visión |
| `robok/vision/` | Detector, huellas de gatos y caras, galería de identidades, seguimiento y escena simulada |
| `robok/web/` | API REST + WebSocket y la interfaz estática |
| `scripts/` | Pruebas de bring-up del hardware (LCD, orientación, demo de la cara, motores, ToF) |
| `system/` | Overlay del LCD para el device tree |
| `docs/` | Diagrama de cableado y su generador |
| `systemd/` | Unidad para el arranque automático (aún no instalada) |
| `models/`, `data/` | Modelos descargados e identidades enseñadas; locales, ignorados por git |
| `LCD-show/` | Driver del fabricante del LCD, con su propio repo; ignorado aquí |

`LCD-show` **sobrescribe `config.txt`** si se vuelve a ejecutar; ver la advertencia en PLAN.md §2.
