import 'package:flutter_test/flutter_test.dart';
import 'package:madeline_mobile/conexion.dart';
import 'package:madeline_mobile/recados_locales.dart';
import 'package:shared_preferences/shared_preferences.dart';

/// Un PC de mentira: guarda los recados en memoria.
class PcFalso extends Conexion {
  final pc = <Map<String, dynamic>>[];
  var siguienteId = 1;

  @override
  Future<List<Map<String, dynamic>>?> recados() async =>
      estado == EstadoConexion.conectada ? List.of(pc) : null;

  @override
  Future<bool> agregarRecado(String texto) async {
    if (estado != EstadoConexion.conectada) return false;
    pc.add({'id': siguienteId++, 'texto': texto});
    return true;
  }

  @override
  Future<bool> borrarRecado(int id) async {
    if (estado != EstadoConexion.conectada) return false;
    pc.removeWhere((r) => r['id'] == id);
    return true;
  }
}

void main() {
  late PcFalso pc;
  late RecadosLocales recados;

  setUp(() {
    SharedPreferences.setMockInitialValues({});
    pc = PcFalso()..estado = EstadoConexion.conectada;
    recados = RecadosLocales(pc);
  });

  test('lo anotado fuera de casa llega al PC al volver', () async {
    await recados.agregar('Pagar la luz');
    await recados.sincronizar();

    pc.estado = EstadoConexion.sinConexion;
    await recados.agregar('Comprar pan');
    final luz = (await recados.lista()).first;
    await recados.tachar(luz);
    expect(await recados.sincronizar(), isFalse);
    expect((await recados.lista()).map((r) => (r.texto, r.pendiente)), [('Comprar pan', true)]);
    expect(await recados.pendientes(), 2);

    pc.estado = EstadoConexion.conectada;
    expect(await recados.sincronizar(), isTrue);
    expect(pc.pc.map((r) => r['texto']), ['Comprar pan']);
    expect((await recados.lista()).single.pendiente, isFalse);
    expect(await recados.pendientes(), 0);
  });

  test('tachar uno que nunca llegó al PC no manda nada', () async {
    pc.estado = EstadoConexion.sinConexion;
    await recados.agregar('Regar las plantas');
    await recados.tachar((await recados.lista()).single);

    pc.estado = EstadoConexion.conectada;
    await recados.sincronizar();
    expect(pc.pc, isEmpty);
  });

  test('trae lo que se agregó en el PC por voz o desde la barra', () async {
    pc.pc.add({'id': 7, 'texto': 'Llamar a la Coni'});
    await recados.sincronizar();
    expect((await recados.lista()).single.texto, 'Llamar a la Coni');
  });
}
