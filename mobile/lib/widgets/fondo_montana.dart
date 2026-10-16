import 'dart:math';

import 'package:flutter/material.dart';

import '../tema.dart';

/// Cielo nocturno con estrellas y la silueta de la montaña al fondo.
class FondoMontana extends StatelessWidget {
  final Widget child;
  const FondoMontana({super.key, required this.child});

  @override
  Widget build(BuildContext context) {
    return CustomPaint(painter: _PintorMontana(), child: child);
  }
}

class _PintorMontana extends CustomPainter {
  @override
  void paint(Canvas canvas, Size size) {
    final rect = Offset.zero & size;
    canvas.drawRect(
      rect,
      Paint()
        ..shader = const LinearGradient(
          begin: Alignment.topCenter,
          end: Alignment.bottomCenter,
          colors: [Colores.cieloArriba, Colores.cieloAbajo],
        ).createShader(rect),
    );

    final azar = Random(7);
    final estrella = Paint();
    for (var i = 0; i < 70; i++) {
      estrella.color = Colors.white.withValues(alpha: 0.25 + azar.nextDouble() * 0.5);
      canvas.drawCircle(
        Offset(azar.nextDouble() * size.width, size.height * (0.13 + azar.nextDouble() * 0.5)),
        azar.nextDouble() * 1.4 + 0.4,
        estrella,
      );
    }

    // Dos cordilleras: la de atrás más clara, como en el ícono.
    final w = size.width, h = size.height;
    final atras = Path()
      ..moveTo(0, h * 0.78)
      ..lineTo(w * 0.22, h * 0.66)
      ..lineTo(w * 0.38, h * 0.72)
      ..lineTo(w * 0.62, h * 0.55)
      ..lineTo(w * 0.80, h * 0.68)
      ..lineTo(w, h * 0.62)
      ..lineTo(w, h)
      ..lineTo(0, h)
      ..close();
    canvas.drawPath(atras, Paint()..color = Colores.montanaAtras.withValues(alpha: 0.55));

    final frente = Path()
      ..moveTo(0, h * 0.86)
      ..lineTo(w * 0.18, h * 0.80)
      ..lineTo(w * 0.42, h * 0.87)
      ..lineTo(w * 0.70, h * 0.78)
      ..lineTo(w, h * 0.85)
      ..lineTo(w, h)
      ..lineTo(0, h)
      ..close();
    canvas.drawPath(frente, Paint()..color = Colores.montanaFrente.withValues(alpha: 0.85));
  }

  @override
  bool shouldRepaint(covariant CustomPainter oldDelegate) => false;
}
