import io
import time
import wave

from robok.services.voice import MOODS, SOUNDS, FakeRunner, Voice, clean_text, tone_wav


def wait_for(cond, timeout=2.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.01)
    return cond()


def test_clean_text_quita_control_y_recorta():
    assert clean_text("  hola\x00\x07   mundo\n\n", 200) == "hola mundo"
    assert clean_text("a" * 500, 50) == "a" * 50
    assert clean_text("\x00\x01  ", 10) == ""


def test_say_encola_y_reproduce_con_el_estado_de_animo():
    runner = FakeRunner()
    v = Voice(runner, voice="es-419")
    v.start()
    assert v.say("Hola, soy Wally", "alegre") is True
    assert wait_for(lambda: runner.spoken)
    v.close()
    text, voice, speed, pitch = runner.spoken[0]
    assert (text, voice) == ("Hola, soy Wally", "es-419")
    assert (speed, pitch) == MOODS["alegre"]


def test_animo_desconocido_usa_normal():
    runner = FakeRunner()
    v = Voice(runner)
    v.start()
    v.say("prueba", "inexistente")
    assert wait_for(lambda: runner.spoken)
    v.close()
    assert runner.spoken[0][2:] == MOODS["normal"]


def test_texto_que_empieza_con_guion_llega_intacto():
    """El texto va por stdin, así que un '-' inicial no puede pasar por opción de espeak-ng."""
    runner = FakeRunner()
    v = Voice(runner)
    v.start()
    v.say("--version && rm -rf /")
    assert wait_for(lambda: runner.spoken)
    v.close()
    assert runner.spoken[0][0] == "--version && rm -rf /"


def test_vacio_o_desactivado_no_encola():
    v = Voice(FakeRunner())
    assert v.say("   ") is False and v.say("\x00") is False
    assert Voice(FakeRunner(), enabled=False).say("hola") is False


def test_cola_llena_descarta_en_vez_de_acumular_retraso():
    v = Voice(FakeRunner(), queue_size=2)       # sin hilo: nadie consume
    assert [v.say(f"f{i}") for i in range(4)] == [True, True, False, False]


def test_stop_vacia_la_cola():
    runner = FakeRunner()
    v = Voice(runner, queue_size=3)
    v.say("uno"); v.say("dos")
    v.stop()
    v.start()
    time.sleep(0.1)
    v.close()
    assert runner.spoken == []


def test_efectos_son_wav_validos_y_se_reproducen():
    for name, steps in SOUNDS.items():
        w = wave.open(io.BytesIO(tone_wav(steps)))
        expected = sum(ms for _, ms in steps) / 1000
        assert abs(w.getnframes() / w.getframerate() - expected) < 0.01, name
    runner = FakeRunner()
    v = Voice(runner)
    v.start()
    assert v.sound("ok") is True and v.sound("no-existe") is False
    assert wait_for(lambda: runner.played)
    v.close()
