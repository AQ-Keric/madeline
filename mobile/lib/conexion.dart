import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:http/http.dart' as http;
import 'package:shared_preferences/shared_preferences.dart';

// El token se configura al compilar, para no dejarlo escrito en el código:
//   flutter build apk --dart-define=MADELINE_TOKEN=xxx
// MADELINE_IP es opcional: la app encuentra al PC sola en la red.
const String kToken = String.fromEnvironment('MADELINE_TOKEN');
const String kIpInicial = String.fromEnvironment('MADELINE_IP');
const int kPuerto = 8000;

const _claveIp = 'ip_servidor';

enum EstadoConexion { buscando, conectada, sinConexion }

/// Encuentra el PC de Madeline en la red y le habla.
///
/// La IP del PC cambia según la red (por cable 192.168.1.x, por WiFi
/// 192.168.0.x...), así que no se deja fija: se prueba la última que
/// funcionó y, si no responde, se busca en la red del celular quién
/// contesta como Madeline en el puerto 8000.
class Conexion {
  String? ip;
  EstadoConexion estado = EstadoConexion.buscando;

  Map<String, String> get cabeceras => {'X-Madeline-Token': kToken};
  String get base => 'http://$ip:$kPuerto';

  /// true si [candidata] es el servidor de Madeline.
  Future<bool> _esMadeline(String candidata, {Duration espera = const Duration(seconds: 2)}) async {
    try {
      final r = await http.get(Uri.parse('http://$candidata:$kPuerto/'), headers: cabeceras).timeout(espera);
      // 401 también es Madeline (con otro token): mejor decirlo que
      // seguir buscando para siempre.
      return r.body.contains('Madeline');
    } catch (_) {
      return false;
    }
  }

  Future<List<String>> _redesLocales() async {
    final prefijos = <String>{};
    final interfaces = await NetworkInterface.list(type: InternetAddressType.IPv4);
    // WiFi primero: la red móvil (rmnet) nunca tiene al PC.
    interfaces.sort((a, b) => (a.name.startsWith('wlan') ? 0 : 1).compareTo(b.name.startsWith('wlan') ? 0 : 1));
    for (final interfaz in interfaces) {
      if (interfaz.name.startsWith('rmnet')) continue;
      for (final dir in interfaz.addresses) {
        final partes = dir.address.split('.');
        if (dir.isLoopback || partes.length != 4) continue;
        prefijos.add('${partes[0]}.${partes[1]}.${partes[2]}');
      }
    }
    return prefijos.toList();
  }

  /// Prueba las 254 direcciones de cada red local en tandas: primero
  /// si el puerto 8000 está abierto (rápido) y después si es Madeline.
  Future<String?> _escanear() async {
    for (final prefijo in await _redesLocales()) {
      final candidatos = List.generate(254, (i) => '$prefijo.${i + 1}');
      for (var i = 0; i < candidatos.length; i += 64) {
        final tanda = candidatos.sublist(i, (i + 64).clamp(0, candidatos.length));
        final abiertos = await Future.wait(tanda.map((c) async {
          try {
            final s = await Socket.connect(c, kPuerto, timeout: const Duration(milliseconds: 600));
            s.destroy();
            return c;
          } catch (_) {
            return null;
          }
        }));
        for (final c in abiertos.whereType<String>()) {
          if (await _esMadeline(c)) return c;
        }
      }
    }
    return null;
  }

  /// Conecta con la última IP conocida o busca al PC. Devuelve el estado.
  Future<EstadoConexion> conectar({bool forzarBusqueda = false}) async {
    estado = EstadoConexion.buscando;
    final prefs = await SharedPreferences.getInstance();

    if (!forzarBusqueda) {
      for (final candidata in {prefs.getString(_claveIp), if (kIpInicial.isNotEmpty) kIpInicial}) {
        if (candidata != null && await _esMadeline(candidata)) {
          ip = candidata;
          return estado = EstadoConexion.conectada;
        }
      }
    }

    final encontrada = await _escanear();
    if (encontrada == null) return estado = EstadoConexion.sinConexion;

    ip = encontrada;
    await prefs.setString(_claveIp, encontrada);
    return estado = EstadoConexion.conectada;
  }

  /// Usa una IP escrita a mano en Ajustes.
  Future<bool> usarIp(String nueva) async {
    nueva = nueva.trim();
    if (!await _esMadeline(nueva, espera: const Duration(seconds: 3))) return false;
    ip = nueva;
    estado = EstadoConexion.conectada;
    final prefs = await SharedPreferences.getInstance();
    await prefs.setString(_claveIp, nueva);
    return true;
  }

  /// Ejecuta una petición y, si el PC no responde (cambió de IP), lo
  /// vuelve a buscar y reintenta una vez.
  Future<http.Response> _conReintento(Future<http.Response> Function() peticion) async {
    try {
      if (ip == null) throw const SocketException('sin IP');
      return await peticion();
    } on SocketException {
      if (await conectar(forzarBusqueda: true) != EstadoConexion.conectada) rethrow;
      return await peticion();
    } on TimeoutException {
      if (await conectar(forzarBusqueda: true) != EstadoConexion.conectada) rethrow;
      return await peticion();
    }
  }

  Future<http.Response> enviarTexto(String texto) => _conReintento(() => http
      .post(
        Uri.parse('$base/comando_texto?origen=telefono'),
        headers: {'Content-Type': 'application/json', ...cabeceras},
        body: jsonEncode({'texto': texto}),
      )
      .timeout(const Duration(seconds: 60)));

  Future<http.Response> enviarAudio(String ruta) => _conReintento(() async {
        final peticion = http.MultipartRequest('POST', Uri.parse('$base/comando_audio?origen=telefono'))
          ..headers.addAll(cabeceras)
          ..files.add(await http.MultipartFile.fromPath('audio', ruta));
        return http.Response.fromStream(await peticion.send().timeout(const Duration(seconds: 60)));
      });

  Future<http.Response> descargar(String ruta) =>
      http.get(Uri.parse('$base$ruta'), headers: cabeceras).timeout(const Duration(seconds: 30));

  /// IDs de las capturas pedidas desde el PC que esperan al celular.
  Future<List<String>> bandeja() async {
    if (ip == null) return [];
    try {
      final r = await http.get(Uri.parse('$base/bandeja'), headers: cabeceras).timeout(const Duration(seconds: 3));
      if (r.statusCode != 200) return [];
      final capturas = (jsonDecode(r.body) as Map<String, dynamic>)['capturas'] as List;
      return capturas.map((c) => c['id'].toString()).toList();
    } catch (_) {
      return [];
    }
  }

  // ------------------------------------------------------------
  // Deck: botones que ejecutan una acción directa en el PC
  // ------------------------------------------------------------

  String iconoApp(String id) => '$base/deck/icono/${Uri.encodeComponent(id)}';

  Future<Map<String, dynamic>?> _getJson(String ruta) async {
    try {
      final r = await _conReintento(
          () => http.get(Uri.parse('$base$ruta'), headers: cabeceras).timeout(const Duration(seconds: 8)));
      return r.statusCode == 200 ? jsonDecode(utf8.decode(r.bodyBytes)) as Map<String, dynamic> : null;
    } catch (_) {
      return null;
    }
  }

  Future<List<Map<String, dynamic>>?> deckApps() async {
    final datos = await _getJson('/deck/apps');
    return (datos?['apps'] as List?)?.cast<Map<String, dynamic>>();
  }

  Future<Map<String, dynamic>?> deckEstado() => _getJson('/deck/estado');

  Future<http.Response> deckAccion(String tool, [Map<String, dynamic> args = const {}]) => _conReintento(() => http
      .post(
        Uri.parse('$base/deck/accion'),
        headers: {'Content-Type': 'application/json', ...cabeceras},
        body: jsonEncode({'tool': tool, 'args': args}),
      )
      .timeout(const Duration(seconds: 30)));

  // ------------------------------------------------------------
  // Recados y agenda (lo mismo que el panel de la barra del PC)
  // ------------------------------------------------------------

  Future<List<Map<String, dynamic>>?> recados() async {
    final datos = await _getJson('/recados');
    return (datos?['recados'] as List?)?.cast<Map<String, dynamic>>();
  }

  Future<bool> agregarRecado(String texto) async {
    try {
      final r = await _conReintento(() => http
          .post(
            Uri.parse('$base/recados'),
            headers: {'Content-Type': 'application/json', ...cabeceras},
            body: jsonEncode({'texto': texto}),
          )
          .timeout(const Duration(seconds: 8)));
      return r.statusCode == 200;
    } catch (_) {
      return false;
    }
  }

  Future<bool> borrarRecado(int id) async {
    try {
      final r = await _conReintento(
          () => http.delete(Uri.parse('$base/recados/$id'), headers: cabeceras).timeout(const Duration(seconds: 8)));
      // 404: ya lo había tachado alguien más (la barra o Madeline).
      return r.statusCode == 200 || r.statusCode == 404;
    } catch (_) {
      return false;
    }
  }

  Future<Map<String, dynamic>?> agenda({int dias = 7}) => _getJson('/agenda?dias=$dias');

  Future<Map<String, dynamic>?> sensores() async {
    if (ip == null) return null;
    try {
      final r = await http.get(Uri.parse('$base/sensores'), headers: cabeceras).timeout(const Duration(seconds: 3));
      return r.statusCode == 200 ? jsonDecode(r.body) as Map<String, dynamic> : null;
    } catch (_) {
      return null;
    }
  }
}
