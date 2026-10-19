import 'dart:convert';

import 'package:shared_preferences/shared_preferences.dart';

import 'conexion.dart';

/// Un recado tal como lo muestra la app. [id] es el del PC; los anotados
/// fuera de casa no tienen hasta que se sincronizan ([pendiente]).
class Recado {
  const Recado({this.id, required this.texto, this.pendiente = false});

  final int? id;
  final String texto;
  final bool pendiente;
}

/// Los recados guardados en el celular, para poder anotar y tachar fuera
/// de casa. Todo cambio queda primero aquí; [sincronizar] lo manda al PC
/// cuando lo encuentra y trae la lista del PC (que también cambia por voz
/// o desde la barra).
class RecadosLocales {
  RecadosLocales(this._conexion);

  final Conexion _conexion;

  static const _claveCopia = 'recados_copia'; // la última lista del PC
  static const _claveNuevos = 'recados_nuevos'; // textos anotados sin conexión
  static const _claveTachados = 'recados_tachados'; // ids tachados sin conexión

  Future<void>? _sincronizando;

  Future<List<String>> _leer(String clave) async =>
      (await SharedPreferences.getInstance()).getStringList(clave) ?? [];

  Future<void> _escribir(String clave, List<String> valor) async =>
      (await SharedPreferences.getInstance()).setStringList(clave, valor);

  /// La lista como debería quedar: la copia del PC sin lo tachado, más lo nuevo.
  Future<List<Recado>> lista() async {
    final tachados = (await _leer(_claveTachados)).toSet();
    return [
      for (final crudo in await _leer(_claveCopia))
        if (jsonDecode(crudo) case {'id': final int id, 'texto': final String texto}
            when !tachados.contains('$id'))
          Recado(id: id, texto: texto),
      for (final texto in await _leer(_claveNuevos)) Recado(texto: texto, pendiente: true),
    ];
  }

  Future<int> pendientes() async => (await _leer(_claveNuevos)).length + (await _leer(_claveTachados)).length;

  Future<void> agregar(String texto) async {
    await _escribir(_claveNuevos, [...await _leer(_claveNuevos), texto]);
  }

  Future<void> tachar(Recado recado) async {
    if (recado.pendiente) {
      // Nunca llegó al PC: basta con olvidarlo aquí.
      final nuevos = await _leer(_claveNuevos);
      nuevos.remove(recado.texto);
      await _escribir(_claveNuevos, nuevos);
    } else {
      await _escribir(_claveTachados, {...await _leer(_claveTachados), '${recado.id}'}.toList());
    }
  }

  /// Manda lo pendiente al PC y trae su lista. true si lo encontró.
  Future<bool> sincronizar() async {
    // Una sola a la vez: la pestaña y la reconexión pueden pedirla juntas.
    final enCurso = _sincronizando;
    if (enCurso != null) {
      await enCurso;
      return _conexion.estado == EstadoConexion.conectada;
    }
    var ok = false;
    final tarea = () async {
      ok = await _sincronizarAhora();
    }();
    _sincronizando = tarea;
    try {
      await tarea;
    } finally {
      _sincronizando = null;
    }
    return ok;
  }

  Future<bool> _sincronizarAhora() async {
    if (_conexion.estado != EstadoConexion.conectada) return false;

    // Uno por uno, y se saca de la cola apenas el PC lo confirma: si se
    // corta a la mitad, lo que faltó se manda la próxima vez.
    for (final texto in await _leer(_claveNuevos)) {
      if (!await _conexion.agregarRecado(texto)) return false;
      final nuevos = await _leer(_claveNuevos);
      nuevos.remove(texto);
      await _escribir(_claveNuevos, nuevos);
    }
    for (final id in await _leer(_claveTachados)) {
      if (!await _conexion.borrarRecado(int.parse(id))) return false;
      final tachados = await _leer(_claveTachados);
      tachados.remove(id);
      await _escribir(_claveTachados, tachados);
    }

    final delPc = await _conexion.recados();
    if (delPc == null) return false;
    await _escribir(_claveCopia, [
      for (final r in delPc) jsonEncode({'id': r['id'], 'texto': r['texto']}),
    ]);
    return true;
  }
}
