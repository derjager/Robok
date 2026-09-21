"""Detector (interpretación de salidas), galería de identidades y seguimiento: piezas puras, sin modelos."""
import json

import numpy as np
import pytest

from robok.vision.detector import CLASS_KINDS, parse_outputs, suppress_containers
from robok.vision.gallery import Gallery, GalleryError, clean_name, slugify
from robok.vision.tracker import Tracker
from robok.vision.types import Detection, crop, iou

KINDS = ("person", "cat", "dog")


def unit(*v):
    a = np.array(v, dtype=np.float32)
    return a / np.linalg.norm(a)


def rand_unit(seed, n=64):
    return unit(*np.random.default_rng(seed).normal(size=n))


# --- detector ---------------------------------------------------------------------------------------------

def test_clases_coco_del_modelo():
    assert CLASS_KINDS == {0: "person", 16: "cat", 17: "dog"}, "el modelo da id COCO - 1"


def test_parse_outputs_convierte_cajas_y_filtra():
    boxes = np.array([[.1, .2, .6, .5], [.0, .0, .3, .3], [.2, .2, .8, .9], [.1, .1, .5, .5]])
    classes = np.array([16, 62, 0, 17.0])          # gato, sofá (no interesa), persona, perro
    scores = np.array([.8, .9, .7, .4])            # el perro no llega al mínimo
    d = parse_outputs(boxes, classes, scores, 4, KINDS, 0.5)
    assert [(x.kind, round(x.score, 2)) for x in d] == [("cat", 0.8), ("person", 0.7)]
    assert d[0].box == pytest.approx((.2, .1, .5, .6)), "ymin,xmin,ymax,xmax -> x0,y0,x1,y1"


def test_parse_outputs_respeta_los_tipos_pedidos_y_la_cantidad():
    boxes = np.array([[.1, .1, .5, .5]] * 3)
    d = parse_outputs(boxes, np.array([16, 0, 17.0]), np.array([.9, .9, .9]), 2, ("cat",), 0.5)
    assert [x.kind for x in d] == ["cat"], "solo gatos, y el tercero está fuera de `count`"


@pytest.mark.parametrize("box", [[.5, .5, .5, .9], [.4, .6, .9, .5], [.0, .0, .02, .02]])
def test_parse_outputs_descarta_cajas_degeneradas_o_diminutas(box):
    assert parse_outputs(np.array([box]), np.array([16.0]), np.array([.9]), 1, KINDS, 0.5) == []


def test_parse_outputs_recorta_cajas_que_se_salen_del_cuadro():
    d = parse_outputs(np.array([[-.1, -.2, 1.2, 1.1]]), np.array([0.0]), np.array([.9]), 1, KINDS, 0.5)
    assert d[0].box == (0.0, 0.0, 1.0, 1.0)


def test_caja_contenedora_de_dos_gatos_se_descarta():
    """Caso real: EfficientDet añade una caja de todo el cuadro además de los dos gatos."""
    a, b = Detection("cat", .72, (.52, .05, .98, .78)), Detection("cat", .67, (.02, .13, .47, .85))
    big = Detection("cat", .52, (0, .05, .99, .99))
    assert suppress_containers([a, b, big]) == [a, b]


def test_un_gato_grande_y_solo_no_se_descarta():
    assert len(suppress_containers([Detection("cat", .6, (0, 0, 1, 1))])) == 1


def test_no_se_descarta_por_contener_a_otro_tipo():
    person, cat = Detection("person", .5, (0, 0, 1, 1)), Detection("cat", .9, (.4, .4, .6, .6))
    assert len(suppress_containers([person, cat])) == 2


# --- tipos -------------------------------------------------------------------------------------------------------

def test_iou_y_recorte():
    assert iou((0, 0, .5, .5), (0, 0, .5, .5)) == 1 and iou((0, 0, .2, .2), (.5, .5, .9, .9)) == 0
    img = np.zeros((100, 200, 3), np.uint8)
    assert crop(img, (.25, .25, .75, .75)).shape[:2] == (50, 100)
    assert crop(img, (.25, .25, .75, .75), pad=1.0).shape[:2] == (100, 200), "el margen se recorta en el borde"
    assert crop(img, (.5, .5, .501, .501)) is None, "recorte diminuto"


# --- galería ---------------------------------------------------------------------------------------------------------

@pytest.fixture
def gal(tmp_path):
    return Gallery(tmp_path / "vision")


def test_crear_identidades_y_nombres_validos(gal):
    a = gal.create("  Pila  ", "cat", follow=True)
    assert (a["id"], a["name"], a["follow"], a["samples"]) == ("g-pila", "Pila", True, [])
    assert gal.create("María José", "person")["id"] == "p-maria-jose", "se quitan acentos para el id"
    assert gal.create("Pila", "person")["id"] == "p-pila", "el mismo nombre puede ser persona y gato"
    with pytest.raises(GalleryError, match="ya existe"):
        gal.create("pila", "cat")
    for bad in ("", "   ", "x" * 33, "a\x00b", "a\x1bb", "a\u202eb"):        # vacío, largo, de control, bidi
        with pytest.raises(GalleryError):
            gal.create(bad, "cat")
    assert gal.create("Mi\ngato\t1", "cat")["name"] == "Mi gato 1", "saltos y tabuladores se vuelven espacios"
    with pytest.raises(GalleryError, match="tipo"):
        gal.create("Rex", "dog")


def test_ids_no_se_repiten_aunque_el_slug_coincida(gal):
    a, b = gal.create("Ñu", "cat"), gal.create("nu!", "cat")
    assert a["id"] != b["id"]


def test_slug_y_limpieza():
    assert slugify("Ángel Ñandú") == "angel-nandu" and slugify("!!!") == "id"
    assert clean_name("a   b") == "a b"


def test_muestras_persisten_y_se_normalizan(gal, tmp_path):
    a = gal.create("Pila", "cat")
    sid = gal.add_sample(a["id"], np.array([3.0, 4.0] + [0.0] * 8), b"jpegbytes")
    g2 = Gallery(tmp_path / "vision")                      # otro proceso: se relee del disco
    ident = g2.get("g-pila")
    assert [s["id"] for s in ident["samples"]] == [sid]
    assert np.linalg.norm(g2.samples_of("g-pila")[0]) == pytest.approx(1.0)
    assert g2.thumb_path("g-pila", sid).read_bytes() == b"jpegbytes"


@pytest.mark.parametrize("bad", [np.zeros(16), np.full(16, np.nan), np.array([1.0, np.inf] + [0.0] * 14), np.ones(3)])
def test_huellas_invalidas_se_rechazan(gal, bad):
    a = gal.create("Pila", "cat")
    with pytest.raises(GalleryError):
        gal.add_sample(a["id"], bad)


def test_no_mezcla_huellas_de_distinto_tamano(gal):
    a = gal.create("Pila", "cat")
    gal.add_sample(a["id"], rand_unit(1, 64))
    with pytest.raises(GalleryError, match="tamaño"):
        gal.add_sample(a["id"], rand_unit(2, 128))


def test_borrar_muestra_e_identidad_limpia_los_archivos(gal, tmp_path):
    a = gal.create("Pila", "cat")
    s1 = gal.add_sample(a["id"], rand_unit(1), b"x")
    s2 = gal.add_sample(a["id"], rand_unit(2), b"y")
    gal.delete_sample(a["id"], s1)
    files = {p.name for p in (tmp_path / "vision" / "samples").iterdir()}
    assert files == {f"{s2}.npy", f"{s2}.jpg"}
    with pytest.raises(GalleryError):
        gal.delete_sample(a["id"], s1)
    gal.delete(a["id"])
    assert list((tmp_path / "vision" / "samples").iterdir()) == [] and gal.list() == []
    with pytest.raises(GalleryError):
        gal.delete("g-pila")


def test_renombrar_y_seguir(gal):
    a, b = gal.create("Pila", "cat"), gal.create("Nube", "cat")
    assert gal.update(a["id"], name="Pilar", follow=True)["name"] == "Pilar"
    with pytest.raises(GalleryError, match="ya existe"):
        gal.update(a["id"], name="NUBE")
    assert gal.update(a["id"], name="Pilar")["name"] == "Pilar", "renombrar a su propio nombre es válido"
    with pytest.raises(GalleryError):
        gal.update("nope", follow=True)


def test_rutas_peligrosas_no_llegan_al_disco(gal):
    a = gal.create("Pila", "cat")
    for sid in ("../../etc/passwd", "zz", "", "a" * 12 + "/x"):
        assert gal.thumb_path(a["id"], sid) is None


def test_gallery_json_corrupto_se_aparta_y_arranca_vacia(tmp_path):
    d = tmp_path / "v"
    d.mkdir()
    (d / "gallery.json").write_text("{esto no es json")
    g = Gallery(d)
    assert g.list() == [] and list(d.glob("gallery.json.corrupto-*")), "el archivo malo no se pierde, se aparta"
    g.create("Pila", "cat")                                  # y se puede seguir usando
    assert Gallery(d).count() == 1


def test_muestra_con_archivo_perdido_se_descarta_sin_tumbar_la_galeria(tmp_path):
    g = Gallery(tmp_path / "v")
    a = g.create("Pila", "cat")
    s1, s2 = g.add_sample(a["id"], rand_unit(1)), g.add_sample(a["id"], rand_unit(2))
    (tmp_path / "v" / "samples" / f"{s1}.npy").unlink()
    (tmp_path / "v" / "samples" / f"{s2}.npy").write_bytes(b"basura")
    assert Gallery(tmp_path / "v").get("g-pila")["samples"] == []


def test_entradas_invalidas_en_el_json_se_ignoran(tmp_path):
    d = tmp_path / "v"
    d.mkdir()
    (d / "gallery.json").write_text(json.dumps({"identities": [
        {"id": "../x", "name": "Mal", "kind": "cat", "samples": []},
        {"id": "g-ok", "name": "Ok", "kind": "cat", "samples": []},
        {"id": "g-tipo", "name": "T", "kind": "dragon", "samples": []}, "no soy un dict"]}))
    assert [i["id"] for i in Gallery(d).list()] == ["g-ok"]


def test_limites(gal, monkeypatch):
    import robok.vision.gallery as gm
    monkeypatch.setattr(gm, "MAX_IDENTITIES", 2)
    gal.create("a", "cat"); gal.create("b", "cat")
    with pytest.raises(GalleryError, match="límite"):
        gal.create("c", "cat")
    monkeypatch.setattr(gm, "MAX_SAMPLES", 1)
    gal.add_sample("g-a", rand_unit(1))
    with pytest.raises(GalleryError, match="límite"):
        gal.add_sample("g-a", rand_unit(2))


# --- coincidencia ------------------------------------------------------------------------------------------------------

def teach(gal, name, vecs, kind="cat"):
    i = gal.create(name, kind)
    for v in vecs:
        gal.add_sample(i["id"], v)
    return i["id"]


def test_match_acepta_al_correcto(gal):
    base = rand_unit(1)
    teach(gal, "Pila", [base, unit(*(base + 0.05 * rand_unit(9)))])
    m = gal.match("cat", unit(*(base + 0.05 * rand_unit(8))), 0.65, 0.05)
    assert m.accepted and m.identity_id == "g-pila" and m.score > 0.95 and m.second == 0.0


def test_match_rechaza_a_un_desconocido_pero_informa_el_mejor_candidato(gal):
    teach(gal, "Pila", [rand_unit(1)])
    m = gal.match("cat", rand_unit(2), 0.65, 0.05)
    assert not m.accepted and m.identity_id is None and m.best_id == "g-pila" and m.score < 0.65


def test_match_exige_margen_entre_dos_gatos_parecidos(gal):
    a = rand_unit(1)
    teach(gal, "Pila", [a])
    teach(gal, "Nube", [unit(*(a + 0.15 * rand_unit(3)))])          # casi igual a Pila
    m = gal.match("cat", a, 0.5, 0.05)
    assert m.best_id == "g-pila" and m.score - m.second < 0.05 and not m.accepted, "ambiguo: no se decide"
    assert gal.match("cat", a, 0.5, 0.0).accepted, "sin margen exigido se aceptaría"


def test_match_distingue_por_tipo(gal):
    v = rand_unit(1)
    teach(gal, "Ana", [v], kind="person")
    assert gal.match("cat", v, 0.5, 0.05).best_id is None, "no compara personas con gatos"
    assert gal.match("person", v, 0.5, 0.05).accepted


def test_match_usa_el_promedio_de_las_3_mejores(gal):
    """Una muestra rara (mal ángulo) no arrastra hacia abajo ni sube a un impostor por sí sola."""
    a, b = rand_unit(1), rand_unit(2)
    teach(gal, "Pila", [a, a, a, b, b, b, b])
    m = gal.match("cat", a, 0.9, 0.0)
    assert m.accepted and m.score == pytest.approx(1.0, abs=1e-5)


def test_match_ignora_huellas_de_otro_tamano_y_entradas_invalidas(gal):
    teach(gal, "Pila", [rand_unit(1, 64)])
    assert gal.match("cat", rand_unit(1, 128), 0.5, 0.0).best_id is None
    for bad in (np.zeros(64), np.array([]), np.full(64, np.nan)):
        assert not gal.match("cat", bad, 0.5, 0.0).accepted


def test_match_sin_identidades(gal):
    m = gal.match("cat", rand_unit(1), 0.5, 0.0)
    assert (m.accepted, m.identity_id, m.best_id, m.score) == (False, None, None, 0.0)


# --- seguimiento ---------------------------------------------------------------------------------------------------------

def det(kind, box, score=0.9):
    return Detection(kind, score, box)


def test_el_mismo_gato_conserva_su_id_al_moverse():
    tr = Tracker()
    ids = []
    for i in range(6):
        x = 0.1 + i * 0.06                                  # se desplaza ~una tercera parte de su ancho por cuadro
        out = tr.update([det("cat", (x, .5, x + .2, .75))], now=i * 0.25)
        ids.append(out[0].id)
    assert len(set(ids)) == 1


def test_dos_gatos_no_intercambian_ids_al_cruzarse_poco():
    tr = Tracker()
    first = {t.id: t.center for t in tr.update([det("cat", (.1, .5, .3, .75)), det("cat", (.6, .5, .8, .75))], 0)}
    out = tr.update([det("cat", (.12, .5, .32, .75)), det("cat", (.58, .5, .78, .75))], 0.25)
    assert {t.id for t in out} == set(first) and len(out) == 2


def test_gato_perdido_reaparece_con_el_mismo_id_si_vuelve_pronto_y_con_otro_si_tarda():
    tr = Tracker(max_age_s=1.0)
    a = tr.update([det("cat", (.4, .5, .6, .75))], 0)[0].id
    assert tr.update([], 0.5) == []
    assert tr.update([det("cat", (.41, .5, .61, .75))], 0.9)[0].id == a
    tr.update([], 1.0); tr.update([], 2.5)
    assert tr.update([det("cat", (.41, .5, .61, .75))], 3.0)[0].id != a


def test_tipos_distintos_no_se_asocian():
    tr = Tracker()
    tr.update([det("cat", (.4, .4, .6, .6))], 0)
    out = tr.update([det("dog", (.4, .4, .6, .6))], 0.25)
    assert out[0].kind == "dog" and out[0].hits == 1


def test_las_personas_conservan_su_pista_mas_tiempo():
    tr = Tracker(max_age_s=1.0, hold_s=8.0)
    p = tr.update([det("person", (.4, .1, .6, .9))], 0)[0]
    p.identity_id = "p-ana"
    for t in (1.0, 4.0, 7.0):
        assert tr.update([], t) == []
    assert tr.update([det("person", (.42, .1, .62, .9))], 7.5)[0].identity_id == "p-ana"


def test_votos_de_identidad_estabilizan_y_tienen_histeresis():
    tr = Tracker()
    t = tr.update([det("cat", (.4, .5, .6, .75))], 0)[0]
    t.add_vote("g-pila", 0.9, 0.0)
    assert t.identity_id is None, "un solo voto no basta"
    t.add_vote("g-pila", 0.8, 0.2)
    assert t.identity_id == "g-pila" and t.identity_score == pytest.approx(0.85)
    t.add_vote("g-nube", 0.7, 0.4)
    assert t.identity_id == "g-pila", "un voto suelto de otro no lo cambia"
    for k in range(4):
        t.add_vote("g-nube", 0.7, 0.6 + k * 0.2)
    assert t.identity_id == "g-nube", "si el otro gana claramente, cambia"


def test_tres_lecturas_sin_reconocer_borran_la_identidad():
    tr = Tracker()
    t = tr.update([det("cat", (.4, .5, .6, .75))], 0)[0]
    t.add_vote("g-pila", .9, 0); t.add_vote("g-pila", .9, .2)
    assert t.identity_id == "g-pila"
    t.votes.clear()
    for k in range(3):
        t.add_vote(None, 0.0, 1 + k)
    assert t.identity_id is None


def test_una_sola_muestra_rara_no_basta_para_aceptar(gal):
    """El promedio de las 3 mejores evita que un impostor parecido a UNA muestra (mal ángulo) pase por Pila."""
    a, outlier = rand_unit(1), rand_unit(7)
    teach(gal, "Pila", [a, unit(*(a + 0.02 * rand_unit(2))), unit(*(a + 0.02 * rand_unit(3))), outlier])
    m = gal.match("cat", outlier, 0.65, 0.0)
    assert not m.accepted and m.score < 0.65, "coincide con una muestra pero con las otras dos mejores no"


def test_histeresis_no_cambia_de_nombre_en_un_empate():
    tr = Tracker()
    t = tr.update([det("cat", (.4, .5, .6, .75))], 0)[0]
    t.add_vote("g-pila", .7, 0); t.add_vote("g-pila", .7, .2)
    assert t.identity_id == "g-pila"
    t.add_vote("g-nube", .95, .4); t.add_vote("g-nube", .95, .6)          # 2 a 2 y Nube con mejor puntuación media
    assert t.identity_id == "g-pila", "en un empate se queda con el que ya tenía"
    t.add_vote("g-nube", .95, .8)
    assert t.identity_id == "g-nube"


def test_parse_outputs_tambien_quita_la_caja_contenedora():
    """Caso real de los dos gatos sobre el sofá: el modelo añade una caja de casi todo el cuadro."""
    boxes = np.array([[.05, .52, .78, .98], [.13, .02, .85, .47], [.05, -.03, .99, .99]])
    d = parse_outputs(boxes, np.array([16.0, 16.0, 16.0]), np.array([.72, .67, .52]), 3, KINDS, 0.5)
    assert [round(x.score, 2) for x in d] == [0.72, 0.67]
