import 'dart:async';
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

enum EstadoAvatar { quieta, escuchando, pensando, hablando }

/// La misma Madeline pixel art del avatar del PC: avatar/generar_sprites.py
/// copia sus cuadros a assets/avatar. Reacciona igual que allá: escucha
/// mientras grabas, piensa mientras espera la respuesta y habla mientras
/// suena su voz; en esos momentos la luz de los audífonos pasa por el
/// arcoíris.
///
/// La luz es un hoyo transparente en cada cuadro (assets/avatar/luz.json
/// dice dónde): se pinta un rectángulo por debajo con el color que toque.
class AvatarMadeline extends StatefulWidget {
  const AvatarMadeline({super.key, required this.estado, this.tamano = 64});

  final EstadoAvatar estado;
  final double tamano;

  /// Primer plano (cabeza y hombros) dentro de un círculo de [diametro],
  /// como foto de perfil.
  static Widget busto({required EstadoAvatar estado, required double diametro, Color? fondo}) {
    // En el sprite la cabeza va de la fila 2 a la 18 y está centrada en
    // la columna 17: se agranda para que esas 16 filas llenen el círculo.
    final escala = diametro / 16;
    return ClipOval(
      child: Container(
        width: diametro,
        height: diametro,
        color: fondo,
        child: Stack(
          clipBehavior: Clip.hardEdge,
          children: [
            Positioned(
              left: diametro / 2 - 17 * escala,
              top: -2 * escala,
              child: AvatarMadeline(estado: estado, tamano: 32 * escala),
            ),
          ],
        ),
      ),
    );
  }

  @override
  State<AvatarMadeline> createState() => _AvatarMadelineState();
}

class _AvatarMadelineState extends State<AvatarMadeline> {
  static Map<String, dynamic>? _luz;
  static const _pestaneo = [0, 1, 0, 1, 0, 1, 0, 2];

  Timer? _reloj;
  int _tic = 0;
  int _cuadro = 0;
  double _tono = 0.75;

  @override
  void initState() {
    super.initState();
    if (_luz == null) {
      rootBundle.loadString('assets/avatar/luz.json').then((texto) {
        _luz = jsonDecode(texto) as Map<String, dynamic>;
        if (mounted) setState(() {});
      });
    }
    _reloj = Timer.periodic(const Duration(milliseconds: 100), (_) => _avanzar());
  }

  @override
  void didChangeDependencies() {
    super.didChangeDependencies();
    // Sin esto el primer cambio de cuadro parpadea mientras carga.
    for (final nombre in _todosLosCuadros) {
      precacheImage(AssetImage('assets/avatar/$nombre.png'), context);
    }
  }

  @override
  void didUpdateWidget(AvatarMadeline anterior) {
    super.didUpdateWidget(anterior);
    if (anterior.estado != widget.estado) _cuadro = 0;
  }

  @override
  void dispose() {
    _reloj?.cancel();
    super.dispose();
  }

  static const _todosLosCuadros = [
    'quieta_0', 'quieta_1', 'quieta_2', 'escuchar_0', 'escuchar_1',
    'pensar_0', 'pensar_1', 'hablar_0', 'hablar_1',
  ];

  String get _animacion => switch (widget.estado) {
        EstadoAvatar.quieta => 'quieta',
        EstadoAvatar.escuchando => 'escuchar',
        EstadoAvatar.pensando => 'pensar',
        EstadoAvatar.hablando => 'hablar',
      };

  // Cada 100 ms; solo redibuja cuando algo cambió (quieta: cada medio
  // segundo, para respirar y pestañear).
  void _avanzar() {
    _tic++;
    final quieta = widget.estado == EstadoAvatar.quieta;
    if (!quieta) _tono = (_tono + 0.04) % 1;
    final cada = switch (widget.estado) {
      EstadoAvatar.hablando => 2,
      EstadoAvatar.quieta => 5,
      _ => 4,
    };
    if (_tic % cada == 0) {
      _cuadro = quieta
          ? _pestaneo[(_pestaneo.indexOf(_cuadro) + 1 + _tic % 2) % _pestaneo.length]
          : (_cuadro + 1) % 2;
    } else if (quieta) {
      return;
    }
    if (mounted) setState(() {});
  }

  Color get _colorLuz {
    if (widget.estado == EstadoAvatar.quieta) {
      final reposo = (_luz?['reposo'] as String?) ?? '#aa78ff';
      return Color(int.parse(reposo.substring(1), radix: 16) | 0xFF000000);
    }
    return HSLColor.fromAHSL(1, _tono * 360, 0.95, 0.62).toColor();
  }

  @override
  Widget build(BuildContext context) {
    final nombre = '${_animacion}_$_cuadro';
    final e = widget.tamano / 32;
    final caja = (_luz?['cuadros']?[nombre] as List?)?.cast<num>();
    return SizedBox(
      width: widget.tamano,
      height: widget.tamano,
      child: Stack(
        children: [
          if (caja != null)
            Positioned(
              left: caja[0] * e,
              top: caja[1] * e,
              width: caja[2] * e,
              height: caja[3] * e,
              child: ColoredBox(color: _colorLuz),
            ),
          Image.asset(
            'assets/avatar/$nombre.png',
            width: widget.tamano,
            height: widget.tamano,
            // Sin fit la imagen nunca se agranda más que sus 32 px.
            fit: BoxFit.fill,
            filterQuality: FilterQuality.none,
            gaplessPlayback: true,
          ),
        ],
      ),
    );
  }
}
