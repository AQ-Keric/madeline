import 'package:flutter/material.dart';

/// Paleta de Madeline: noche en la montaña de Celeste, su pelo morado
/// y su sudadera lila (como el avatar del PC), el azul del pelo después
/// del dash, fresas y nieve.
class Colores {
  static const cieloArriba = Color(0xFF140C2E);
  static const cieloAbajo = Color(0xFF2E1A4A);
  static const montanaAtras = Color(0xFF3A2463);
  static const montanaFrente = Color(0xFF26173F);
  static const superficie = Color(0xFF241640);
  static const superficieAlta = Color(0xFF32205A);
  static const borde = Color(0xFF4A3478);

  static const pelo = Color(0xFF8E52E0); // morado de Madeline
  static const sudadera = Color(0xFFB0A0D6);
  static const dash = Color(0xFF5BB4F0); // azul tras el dash: grabando
  static const peligro = Color(0xFFE5484D); // errores, apagar, sin conexión
  static const fresa = Color(0xFFFF6B81);
  static const nieve = Color(0xFFF4EEFF);
  static const texto = Color(0xFFEDE6FF);
  static const textoSuave = Color(0xFFA99BD0);
  static const ok = Color(0xFF6EE7A8);
  static const aviso = Color(0xFFFFC56B);
}

ThemeData temaMadeline() {
  final base = ThemeData(brightness: Brightness.dark, useMaterial3: true);
  return base.copyWith(
    scaffoldBackgroundColor: Colores.cieloArriba,
    colorScheme: base.colorScheme.copyWith(
      primary: Colores.pelo,
      secondary: Colores.dash,
      surface: Colores.superficie,
      onSurface: Colores.texto,
    ),
    textTheme: base.textTheme.apply(bodyColor: Colores.texto, displayColor: Colores.texto),
    filledButtonTheme: FilledButtonThemeData(
      style: FilledButton.styleFrom(backgroundColor: Colores.pelo, foregroundColor: Colors.white),
    ),
    outlinedButtonTheme: OutlinedButtonThemeData(
      style: OutlinedButton.styleFrom(foregroundColor: Colores.texto, side: const BorderSide(color: Colores.borde)),
    ),
    snackBarTheme: const SnackBarThemeData(
      backgroundColor: Colores.superficieAlta,
      contentTextStyle: TextStyle(color: Colores.texto),
      behavior: SnackBarBehavior.floating,
    ),
    bottomSheetTheme: const BottomSheetThemeData(
      backgroundColor: Colores.superficie,
      showDragHandle: true,
      dragHandleColor: Colores.borde,
    ),
  );
}
