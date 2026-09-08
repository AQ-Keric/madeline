import 'dart:async';
import 'dart:convert';
import 'package:flutter/material.dart';
import 'package:http/http.dart' as http;
import 'package:path_provider/path_provider.dart';
import 'package:permission_handler/permission_handler.dart';
import 'package:record/record.dart';
import 'package:audioplayers/audioplayers.dart';

void main() {
  runApp(const JarvisApp());
}

class JarvisApp extends StatelessWidget {
  const JarvisApp({Key? key}) : super(key: key);

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'J.A.R.V.I.S. Uplink',
      theme: ThemeData(
        brightness: Brightness.dark,
        primaryColor: Colors.purpleAccent,
        scaffoldBackgroundColor: const Color(0xFF050505),
      ),
      home: const JarvisHome(),
      debugShowCheckedModeBanner: false,
    );
  }
}

class JarvisHome extends StatefulWidget {
  const JarvisHome({Key? key}) : super(key: key);

  @override
  State<JarvisHome> createState() => _JarvisHomeState();
}

class _JarvisHomeState extends State<JarvisHome> {
  final String serverIp = '192.168.0.197'; 
  final String serverPort = '8000';

  late final AudioRecorder _audioRecorder;
  final AudioPlayer _audioPlayer = AudioPlayer();
  final TextEditingController _textController = TextEditingController();
  
  bool _isRecording = false;
  bool _isProcessing = false;
  
  String _statusText = 'Iniciando enlace...';
  String _responseText = 'Iniciando protocolo de conexión con CachyOS...';
  String? _imageUrl;

  @override
  void initState() {
    super.initState();
    _audioRecorder = AudioRecorder();
    _checkConnection();
  }

  // --- NUEVA FUNCIÓN DE VERIFICACIÓN REAL ---
  Future<void> _checkConnection() async {
    final uri = Uri.parse('http://$serverIp:$serverPort/');
    try {
      // Intentamos hacer un ping al PC. Si en 3 segundos no responde, asume que está caído.
      final response = await http.get(uri).timeout(const Duration(seconds: 3));
      
      if (response.statusCode == 200) {
        setState(() {
          _responseText = 'Enlace con CachyOS establecido y verificado. Sistemas en línea, señor.';
          _statusText = 'Listo para recibir órdenes';
        });
      } else {
        setState(() {
          _responseText = 'Error de conexión: El servidor respondió con código ${response.statusCode}.';
          _statusText = 'Desconectado';
        });
      }
    } catch (e) {
      setState(() {
        _responseText = 'Fallo crítico de enlace.\n\nServidor CachyOS inalcanzable en la IP $serverIp.\nRevise que el script de Jarvis esté corriendo en la terminal y que el celular esté en el mismo WiFi.';
        _statusText = 'Sin conexión';
      });
    }
  }


  @override
  void dispose() {
    _audioRecorder.dispose();
    _audioPlayer.dispose();
    _textController.dispose();
    super.dispose();
  }

  Future<void> _startRecording() async {
    try {
      if (await Permission.microphone.request().isGranted) {
        final dir = await getTemporaryDirectory();
        final path = '${dir.path}/jarvis_mobile.wav';

        await _audioRecorder.start(
          const RecordConfig(encoder: AudioEncoder.wav),
          path: path,
        );

        setState(() {
          _isRecording = true;
          _statusText = 'Escuchando...';
          _responseText = '';
          _imageUrl = null;
        });
      }
    } catch (e) {
      setState(() => _statusText = 'Error al grabar: $e');
    }
  }

  Future<void> _stopRecordingAndSend() async {
    try {
      final path = await _audioRecorder.stop();
      setState(() {
        _isRecording = false;
        _isProcessing = true;
        _statusText = 'Procesando audio...';
      });

      if (path != null) await _sendAudioToServer(path);
    } catch (e) {}
  }

  Future<void> _sendAudioToServer(String filePath) async {
    final uri = Uri.parse('http://$serverIp:$serverPort/comando_audio?origen=telefono');
    try {
      var request = http.MultipartRequest('POST', uri);
      request.files.add(await http.MultipartFile.fromPath('audio', filePath));
      request.headers.addAll({'accept': 'application/json'});

      var streamedResponse = await request.send();
      var response = await http.Response.fromStream(streamedResponse);
      await _handleServerResponse(response);
    } catch (e) {
      setState(() => _responseText = 'Error de conexión: $e');
    } finally {
      setState(() {
        _isProcessing = false;
        _statusText = 'Listo para recibir órdenes';
      });
    }
  }

  Future<void> _sendTextToServer(String text) async {
    if (text.trim().isEmpty) return;
    FocusScope.of(context).unfocus();
    _textController.clear();

    setState(() {
      _isProcessing = true;
      _statusText = 'Enviando texto...';
      _responseText = '';
      _imageUrl = null;
    });

    final uri = Uri.parse('http://$serverIp:$serverPort/comando_texto?origen=telefono');
    try {
      var response = await http.post(
        uri,
        headers: {"Content-Type": "application/json", "accept": "application/json"},
        body: jsonEncode({"texto": text}),
      );
      await _handleServerResponse(response);
    } catch (e) {
      setState(() => _responseText = 'Error de conexión por texto.');
    } finally {
      setState(() {
        _isProcessing = false;
        _statusText = 'Listo para recibir órdenes';
      });
    }
  }

  Future<void> _handleServerResponse(http.Response response) async {
    if (response.statusCode == 200) {
      final data = jsonDecode(utf8.decode(response.bodyBytes));
      setState(() {
        _responseText = data['response'] ?? 'Sin respuesta';
        if (data['image_path'] != null) {
          final time = DateTime.now().millisecondsSinceEpoch;
          _imageUrl = 'http://$serverIp:$serverPort${data['image_path']}?t=$time';
        } else {
          _imageUrl = null;
        }
      });

      if (data['audio_path'] != null) {
        final time = DateTime.now().millisecondsSinceEpoch;
        final String audioUrl = 'http://$serverIp:$serverPort${data['audio_path']}?t=$time';
        try {
          await _audioPlayer.stop();
          await _audioPlayer.play(UrlSource(audioUrl));
        } catch (e) {
          print('[!] Error reproduciendo audio de Jarvis: $e');
        }
      }
    } else {
      setState(() => _responseText = 'Error del servidor: ${response.statusCode}');
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: const Text('J.A.R.V.I.S.', style: TextStyle(fontWeight: FontWeight.bold, letterSpacing: 2, color: Colors.purpleAccent)),
        centerTitle: true,
        elevation: 0,
        backgroundColor: Colors.transparent,
      ),
      endDrawer: MonitorDrawer(serverIp: serverIp, serverPort: serverPort),
      body: SafeArea(
        child: Column(
          children: [
            Expanded(
              child: Container(
                width: double.infinity,
                margin: const EdgeInsets.symmetric(horizontal: 20, vertical: 10),
                padding: const EdgeInsets.all(20),
                decoration: BoxDecoration(
                  color: const Color(0xFF111111), 
                  borderRadius: BorderRadius.circular(20),
                  border: Border.all(color: Colors.purpleAccent.withOpacity(0.5), width: 1.5),
                  boxShadow: [
                    BoxShadow(
                      color: Colors.purpleAccent.withOpacity(0.1),
                      blurRadius: 15,
                      spreadRadius: 2,
                    ),
                  ],
                ),
                child: SingleChildScrollView(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      const Text("TERMINAL:", style: TextStyle(color: Colors.purpleAccent, fontSize: 12, letterSpacing: 2)),
                      const SizedBox(height: 10),
                      Text(_responseText, style: const TextStyle(fontSize: 18, height: 1.5, color: Colors.white70)),
                      const SizedBox(height: 20),
                      if (_imageUrl != null)
                        ClipRRect(
                          borderRadius: BorderRadius.circular(10),
                          child: Image.network(_imageUrl!, fit: BoxFit.cover),
                        ),
                    ],
                  ),
                ),
              ),
            ),
            Padding(
              padding: const EdgeInsets.symmetric(vertical: 20),
              child: Column(
                children: [
                  Text(
                    _isProcessing ? _statusText : "Mantén presionado para hablar",
                    style: TextStyle(color: _isProcessing ? Colors.purpleAccent : Colors.grey, fontWeight: FontWeight.bold),
                  ),
                  const SizedBox(height: 15),
                  GestureDetector(
                    onTapDown: (_) => _startRecording(),
                    onTapUp: (_) => _stopRecordingAndSend(),
                    onTapCancel: () => _stopRecordingAndSend(),
                    child: AnimatedContainer(
                      duration: const Duration(milliseconds: 200),
                      height: _isRecording ? 100 : 85,
                      width: _isRecording ? 100 : 85,
                      decoration: BoxDecoration(
                        color: _isRecording ? Colors.redAccent : Colors.purpleAccent,
                        shape: BoxShape.circle,
                        boxShadow: [
                          BoxShadow(
                            color: _isRecording ? Colors.redAccent.withOpacity(0.6) : Colors.purpleAccent.withOpacity(0.4),
                            blurRadius: _isRecording ? 25 : 15,
                            spreadRadius: _isRecording ? 5 : 2,
                          ),
                        ],
                      ),
                      child: Icon(Icons.mic, color: Colors.white, size: _isRecording ? 45 : 40),
                    ),
                  ),
                ],
              ),
            ),
            Padding(
              padding: const EdgeInsets.fromLTRB(20, 0, 20, 20),
              child: Row(
                children: [
                  Expanded(
                    child: TextField(
                      controller: _textController,
                      style: const TextStyle(color: Colors.white),
                      decoration: InputDecoration(
                        hintText: 'O escribe un comando...',
                        hintStyle: const TextStyle(color: Colors.white38),
                        filled: true,
                        fillColor: const Color(0xFF1E1E1E), 
                        border: OutlineInputBorder(borderRadius: BorderRadius.circular(30), borderSide: BorderSide.none),
                        contentPadding: const EdgeInsets.symmetric(horizontal: 20, vertical: 15),
                      ),
                      onSubmitted: (value) => _sendTextToServer(value),
                    ),
                  ),
                  const SizedBox(width: 10),
                  Container(
                    decoration: BoxDecoration(
                      color: const Color(0xFF1E1E1E), 
                      shape: BoxShape.circle,
                      border: Border.all(color: Colors.purpleAccent.withOpacity(0.3), width: 1),
                    ),
                    child: IconButton(
                      icon: const Icon(Icons.send, color: Colors.purpleAccent),
                      onPressed: () => _sendTextToServer(_textController.text),
                    ),
                  ),
                ],
              ),
            ),
          ],
        ),
      ),
    );
  }
}

class MonitorDrawer extends StatefulWidget {
  final String serverIp;
  final String serverPort;

  const MonitorDrawer({Key? key, required this.serverIp, required this.serverPort}) : super(key: key);

  @override
  State<MonitorDrawer> createState() => _MonitorDrawerState();
}

class _MonitorDrawerState extends State<MonitorDrawer> {
  Timer? _timer;
  Map<String, dynamic> _sensores = {};
  bool _isLoading = true;

  @override
  void initState() {
    super.initState();
    _fetchSensores();
    _timer = Timer.periodic(const Duration(seconds: 2), (timer) => _fetchSensores());
  }

  @override
  void dispose() {
    _timer?.cancel();
    super.dispose();
  }

  Future<void> _fetchSensores() async {
    final uri = Uri.parse('http://${widget.serverIp}:${widget.serverPort}/sensores');
    try {
      final response = await http.get(uri).timeout(const Duration(seconds: 2));
      if (response.statusCode == 200) {
        if (mounted) {
          setState(() {
            _sensores = jsonDecode(response.body);
            _isLoading = false;
          });
        }
      }
    } catch (e) {
    }
  }

  Widget _buildSensorCard(String titulo, String valor, String subtitulo, IconData icono, Color color) {
    return Container(
      margin: const EdgeInsets.symmetric(vertical: 8),
      padding: const EdgeInsets.all(15),
      decoration: BoxDecoration(
        color: const Color(0xFF1E1E1E),
        borderRadius: BorderRadius.circular(15),
        border: Border.all(color: color.withOpacity(0.3), width: 1.5),
        boxShadow: [
          BoxShadow(color: color.withOpacity(0.1), blurRadius: 10, spreadRadius: 1),
        ],
      ),
      child: Row(
        children: [
          Icon(icono, color: color, size: 30),
          const SizedBox(width: 15),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(titulo, style: const TextStyle(color: Colors.white70, fontSize: 12, fontWeight: FontWeight.bold)),
                Text(valor, style: TextStyle(color: color, fontSize: 22, fontWeight: FontWeight.bold)),
                Text(subtitulo, style: const TextStyle(color: Colors.white38, fontSize: 10)),
              ],
            ),
          ),
        ],
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    return Drawer(
      backgroundColor: const Color(0xFF050505),
      child: SafeArea(
        child: Padding(
          padding: const EdgeInsets.all(20.0),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              const Row(
                children: [
                  Icon(Icons.monitor_heart, color: Colors.purpleAccent),
                  SizedBox(width: 10),
                  Text("DIAGNÓSTICO", style: TextStyle(color: Colors.purpleAccent, fontSize: 18, fontWeight: FontWeight.bold, letterSpacing: 2)),
                ],
              ),
              const Divider(color: Colors.white24, height: 30),
              
              if (_isLoading)
                const Expanded(child: Center(child: CircularProgressIndicator(color: Colors.purpleAccent)))
              else
                Expanded(
                  child: ListView(
                    physics: const BouncingScrollPhysics(),
                    children: [
                      _buildSensorCard(
                        "USO DE CPU", 
                        "${_sensores['cpu_uso'] ?? '0'}%", 
                        "Temp: ${_sensores['cpu_temp'] ?? 'N/A'} °C", 
                        Icons.memory, 
                        Colors.blueAccent
                      ),
                      _buildSensorCard(
                        "GPU NVIDIA", 
                        "${_sensores['gpu_uso'] ?? '0'}%", 
                        "Temp: ${_sensores['gpu_temp'] ?? 'N/A'} °C", 
                        Icons.videogame_asset, 
                        Colors.greenAccent
                      ),
                      _buildSensorCard(
                        "MEMORIA RAM", 
                        "${_sensores['ram_usada'] ?? '0'} MB", 
                        "De ${_sensores['ram_total'] ?? '0'} MB total", 
                        Icons.storage, 
                        Colors.orangeAccent
                      ),
                    ],
                  ),
                ),
            ],
          ),
        ),
      ),
    );
  }
}