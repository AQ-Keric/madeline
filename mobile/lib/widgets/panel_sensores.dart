import 'dart:async';

import 'package:flutter/material.dart';

import '../conexion.dart';
import '../tema.dart';

/// Estado del PC en tiempo real (hoja que sube desde abajo).
class PanelSensores extends StatefulWidget {
  final Conexion conexion;
  const PanelSensores({super.key, required this.conexion});

  @override
  State<PanelSensores> createState() => _PanelSensoresState();
}

class _PanelSensoresState extends State<PanelSensores> {
  Timer? _timer;
  Map<String, dynamic>? _datos;
  bool _fallo = false;

  @override
  void initState() {
    super.initState();
    _actualizar();
    _timer = Timer.periodic(const Duration(seconds: 2), (_) => _actualizar());
  }

  @override
  void dispose() {
    _timer?.cancel();
    super.dispose();
  }

  Future<void> _actualizar() async {
    final datos = await widget.conexion.sensores();
    if (!mounted) return;
    setState(() {
      _fallo = datos == null;
      _datos = datos ?? _datos;
    });
  }

  double _porcentaje(String? valor) => ((double.tryParse(valor ?? '') ?? 0) / 100).clamp(0, 1);

  Widget _tarjeta(String titulo, String valor, String detalle, double progreso, IconData icono, Color color) {
    return Container(
      margin: const EdgeInsets.only(bottom: 12),
      padding: const EdgeInsets.all(16),
      decoration: BoxDecoration(
        color: Colores.superficieAlta.withValues(alpha: 0.6),
        borderRadius: BorderRadius.circular(16),
        border: Border.all(color: Colores.borde),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              Icon(icono, color: color, size: 22),
              const SizedBox(width: 10),
              Text(titulo, style: const TextStyle(color: Colores.textoSuave, fontWeight: FontWeight.w600)),
              const Spacer(),
              Text(valor, style: TextStyle(color: color, fontSize: 20, fontWeight: FontWeight.bold)),
            ],
          ),
          const SizedBox(height: 12),
          ClipRRect(
            borderRadius: BorderRadius.circular(6),
            child: LinearProgressIndicator(
              value: progreso,
              minHeight: 8,
              color: color,
              backgroundColor: Colores.borde.withValues(alpha: 0.5),
            ),
          ),
          const SizedBox(height: 8),
          Text(detalle, style: const TextStyle(color: Colores.textoSuave, fontSize: 12)),
        ],
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    final d = _datos;
    return SafeArea(
      child: Padding(
        padding: const EdgeInsets.fromLTRB(20, 0, 20, 12),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                const Text('Estado del PC', style: TextStyle(fontSize: 20, fontWeight: FontWeight.bold)),
                const Spacer(),
                if (_fallo)
                  const Text('sin datos', style: TextStyle(color: Colores.aviso, fontSize: 12))
                else
                  const Text('en vivo', style: TextStyle(color: Colores.ok, fontSize: 12)),
              ],
            ),
            const SizedBox(height: 16),
            if (d == null)
              const Padding(
                padding: EdgeInsets.all(32),
                child: Center(child: CircularProgressIndicator(color: Colores.pelo)),
              )
            else ...[
              _tarjeta('CPU', '${d['cpu_uso'] ?? 0}%', 'Temperatura: ${d['cpu_temp'] ?? 'N/A'} °C',
                  _porcentaje('${d['cpu_uso']}'), Icons.memory, Colores.dash),
              _tarjeta('GPU', '${d['gpu_uso'] ?? 0}%', 'Temperatura: ${d['gpu_temp'] ?? 'N/A'} °C',
                  _porcentaje('${d['gpu_uso']}'), Icons.videogame_asset, Colores.ok),
              _tarjeta(
                'RAM',
                '${d['ram_usada'] ?? 0} MB',
                'de ${d['ram_total'] ?? 0} MB',
                ((double.tryParse('${d['ram_usada']}') ?? 0) / (double.tryParse('${d['ram_total']}') ?? 1)).clamp(0, 1),
                Icons.storage,
                Colores.fresa,
              ),
            ],
          ],
        ),
      ),
    );
  }
}
