import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import 'pantalla_principal.dart';
import 'tema.dart';

void main() {
  WidgetsFlutterBinding.ensureInitialized();
  SystemChrome.setSystemUIOverlayStyle(const SystemUiOverlayStyle(
    statusBarColor: Colors.transparent,
    systemNavigationBarColor: Colores.montanaFrente,
  ));
  runApp(const MadelineApp());
}

class MadelineApp extends StatelessWidget {
  const MadelineApp({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'Madeline',
      theme: temaMadeline(),
      home: const PantallaPrincipal(),
      debugShowCheckedModeBanner: false,
    );
  }
}
