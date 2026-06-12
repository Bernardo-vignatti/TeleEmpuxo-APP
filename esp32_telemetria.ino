/*
  ================================================================
  TELEMETRIA DE EMPUXO — ESP32 + HX711  v2.5
  ================================================================
  ARQUITETURA:
    ESP32 → WebSocket (cliente) → server.py → Overlay / Dashboard

  PRIMEIRO USO / TROCAR REDE:
    1. Segure BOOT ao ligar  OU  compile com FORCE_PORTAL = true
    2. Aparece o AP "Telemetria-Config" (senha: 12345678)
    3. Conecte no celular/notebook nessa rede
    4. Acesse http://192.168.4.1
    5. Clique em "Configurar WiFi"
    6. Escolha a rede, coloque a senha
    7. Preencha o campo "IP do Servidor Python"
    8. Clique Salvar — o ESP32 reinicia e conecta

  USO NORMAL (rede ja salva):
    - Ligue normalmente, ele conecta sozinho

  DEPENDENCIAS (Library Manager):
    - HX711 by Bogdan Necula
    - WebSockets by Markus Sattler
    - WiFiManager by tzapu
    - ArduinoJson by Benoit Blanchon
  ================================================================
*/

#include <WiFi.h>
#include <WiFiManager.h>
#include <WebSocketsClient.h>
#include <HX711.h>
#include <ArduinoJson.h>
#include <Preferences.h>

// ── Forcar portal de configuracao (util para debug) ───────────
#define FORCE_PORTAL  true

// ── Pinos HX711 ──────────────────────────────────────────────
#define DOUT_PIN     15
#define SCK_PIN       5
#define BOOT_BTN      0

// ── Pinos LEDs ────────────────────────────────────────────────
// LED VERDE   → ligado direto no VCC, sempre aceso (alimentacao OK)
// LED AMARELO → estado da conexao / sessao (controlado por software)
// LED VERMELHO → estado da queima / perigo (controlado por software)
#define LED_YELLOW   18
#define LED_RED      19

// ── Configuracoes de comportamento dos LEDs ───────────────────
#define LED_COOLDOWN_MS      15000UL  // 15 s pos-queima
#define LED_BLINK_SLOW_MS      800UL  // pisca lento (idle conectado)
#define LED_BLINK_FAST_MS      150UL  // pisca rapido (erro / sem conexao)
#define LED_BLINK_COOL_MS      400UL  // pisca pos-queima
#define LED_HEARTBEAT_ENABLED  true
#define LED_HEARTBEAT_PULSE_MS   8UL  // duracao do pulso heartbeat (ms)

// ── Configuracoes gerais ──────────────────────────────────────
#define AP_NAME          "Telemetria-Config"
#define AP_PASS          "12345678"
#define SERVER_PORT      8765
#define SEND_INTERVAL_MS 20           // 50 Hz

#define CAL_FACTOR       -2280.0f     // ajuste ate bater com peso conhecido
#define EMA_ALPHA        0.15f        // suavizacao exponencial (0=max suave)
#define NOISE_FLOOR      0.25f        // dead-band em Newtons

#define RECONNECT_MS     5000
#define WIFI_RECONNECT_S 10000

// ── Maquina de estados dos LEDs ───────────────────────────────
enum LedState {
  LED_IDLE_DISCONNECTED,  // amarelo pisca rapido — sem WiFi/WS
  LED_IDLE_CONNECTED,     // amarelo pisca lento  — conectado, aguardando
  LED_SESSION_ARMED,      // heartbeat no amarelo — sessao ativa
  LED_BURNING,            // vermelho solido       — queima detectada
  LED_COOLDOWN,           // vermelho pisca        — motor quente
  LED_ERROR_LINK_LOST     // ambos piscam rapido   — WS perdido em sessao
};

// ── Objetos globais ───────────────────────────────────────────
HX711            scale;
WebSocketsClient wsClient;
Preferences      prefs;

// ── Variaveis de estado ───────────────────────────────────────
char     serverIP[41]      = "192.168.1.100";
bool     wsConnected       = false;
bool     sessionActive     = false;
float    filtered          = 0.0f;
unsigned long lastSend     = 0;
unsigned long lastWifiChk  = 0;

LedState      ledState          = LED_IDLE_DISCONNECTED;
bool          ledYellowOn       = false;
bool          ledRedOn          = false;
unsigned long ledLastToggle     = 0;
unsigned long cooldownStart     = 0;
unsigned long heartbeatUntil    = 0;

WiFiManagerParameter* paramIP_ptr = nullptr;

// ── Callbacks WiFiManager ─────────────────────────────────────
void saveParamCallback() {
  if (paramIP_ptr) {
    String ip = paramIP_ptr->getValue();
    ip.trim();
    if (ip.length() > 0) {
      ip.toCharArray(serverIP, sizeof(serverIP));
      prefs.begin("telemetria", false);
      prefs.putString("server_ip", serverIP);
      prefs.end();
      Serial.printf("[WiFi] IP salvo: %s\n", serverIP);
    }
  }
}

void loadServerIP() {
  prefs.begin("telemetria", true);
  String saved = prefs.getString("server_ip", "");
  prefs.end();
  if (saved.length() > 0) {
    saved.toCharArray(serverIP, sizeof(serverIP));
    Serial.printf("[Prefs] IP carregado: %s\n", serverIP);
  }
}

// ── Aplicar estado dos LEDs ao hardware ───────────────────────
void applyLEDs() {
  digitalWrite(LED_YELLOW, ledYellowOn ? HIGH : LOW);
  digitalWrite(LED_RED,    ledRedOn    ? HIGH : LOW);
}

// ── Heartbeat: pulso breve no amarelo a cada leitura HX711 ────
void triggerHeartbeat() {
  if (!LED_HEARTBEAT_ENABLED) return;
  if (ledState == LED_SESSION_ARMED) {
    heartbeatUntil = millis() + LED_HEARTBEAT_PULSE_MS;
  }
}

// ── Maquina de estados LED (chamada no loop, nao bloqueia) ────
void tickLEDs() {
  unsigned long now = millis();

  // Verifica timeout de cooldown
  if (ledState == LED_COOLDOWN) {
    if (now - cooldownStart >= LED_COOLDOWN_MS) {
      ledState      = wsConnected ? LED_IDLE_CONNECTED : LED_IDLE_DISCONNECTED;
      ledLastToggle = now;
      Serial.println("[LED] Cooldown encerrado.");
    }
  }

  switch (ledState) {

    case LED_IDLE_DISCONNECTED:
      if (now - ledLastToggle >= LED_BLINK_FAST_MS / 2) {
        ledLastToggle = now;
        ledYellowOn   = !ledYellowOn;
      }
      ledRedOn = false;
      applyLEDs();
      break;

    case LED_IDLE_CONNECTED:
      if (now - ledLastToggle >= LED_BLINK_SLOW_MS / 2) {
        ledLastToggle = now;
        ledYellowOn   = !ledYellowOn;
      }
      ledRedOn = false;
      applyLEDs();
      break;

    case LED_SESSION_ARMED:
      // Amarelo apagado; pulso breve (heartbeat) a cada leitura HX711
      if (LED_HEARTBEAT_ENABLED && now < heartbeatUntil) {
        ledYellowOn = true;
      } else {
        ledYellowOn = false;
      }
      ledRedOn = false;
      applyLEDs();
      break;

    case LED_BURNING:
      ledYellowOn = false;
      ledRedOn    = true;
      applyLEDs();
      break;

    case LED_COOLDOWN:
      // Vermelho pisca — motor quente, nao toque!
      if (now - ledLastToggle >= LED_BLINK_COOL_MS / 2) {
        ledLastToggle = now;
        ledRedOn      = !ledRedOn;
      }
      ledYellowOn = false;
      applyLEDs();
      break;

    case LED_ERROR_LINK_LOST:
      // Amarelo e vermelho piscam juntos — WS perdido em sessao ativa
      if (now - ledLastToggle >= LED_BLINK_FAST_MS / 2) {
        ledLastToggle = now;
        ledYellowOn   = !ledYellowOn;
        ledRedOn      = ledYellowOn;
      }
      applyLEDs();
      break;
  }
}

// ── Handler de eventos WebSocket ──────────────────────────────
void onWsEvent(WStype_t type, uint8_t* payload, size_t length) {
  switch (type) {

    case WStype_DISCONNECTED:
      wsConnected = false;
      Serial.println("[WS] Desconectado.");
      if (sessionActive) {
        ledState      = LED_ERROR_LINK_LOST;
        ledLastToggle = millis();
      } else {
        ledState      = LED_IDLE_DISCONNECTED;
        ledLastToggle = millis();
      }
      break;

    case WStype_CONNECTED:
      wsConnected = true;
      Serial.printf("[WS] Conectado: %s\n", payload);
      ledState      = sessionActive ? LED_SESSION_ARMED : LED_IDLE_CONNECTED;
      ledLastToggle = millis();
      wsClient.sendTXT("{\"hello\":\"esp32\",\"version\":\"2.5\"}");
      break;

    case WStype_TEXT: {
      StaticJsonDocument<128> rx;
      if (!deserializeJson(rx, payload, length)) {

        if (rx.containsKey("led")) {
          const char* cmd = rx["led"];
          if (strcmp(cmd, "armed") == 0) {
            sessionActive = true;
            ledState      = LED_SESSION_ARMED;
            Serial.println("[LED] Sessao armada.");
          } else if (strcmp(cmd, "burning") == 0) {
            ledState = LED_BURNING;
            Serial.println("[LED] QUEIMA ATIVA.");
          } else if (strcmp(cmd, "cooldown") == 0) {
            cooldownStart = millis();
            ledState      = LED_COOLDOWN;
            ledLastToggle = millis();
            Serial.printf("[LED] Cooldown iniciado (%lu s).\n",
                          LED_COOLDOWN_MS / 1000);
          } else if (strcmp(cmd, "idle") == 0) {
            if (ledState == LED_COOLDOWN) {
              Serial.println("[LED] Idle ignorado — cooldown ainda ativo.");
            } else {
              sessionActive = false;
              ledState      = wsConnected ? LED_IDLE_CONNECTED
                                          : LED_IDLE_DISCONNECTED;
              ledLastToggle = millis();
              Serial.println("[LED] Idle.");
            }
          }
        }

        // Tara remota via WebSocket
        if (rx.containsKey("tare") && rx["tare"].as<bool>()) {
          scale.tare(5);
          Serial.println("[HX711] Tara remota aplicada.");
        }
      }
      break;
    }

    case WStype_PING:
    case WStype_PONG:
      break;

    default:
      break;
  }
}

// ── Sequencia de boot: pisca alternado amarelo/vermelho 3x ────
void bootSequence() {
  for (int i = 0; i < 3; i++) {
    digitalWrite(LED_YELLOW, HIGH); digitalWrite(LED_RED, LOW);  delay(120);
    digitalWrite(LED_YELLOW, LOW);  digitalWrite(LED_RED, HIGH); delay(120);
  }
  digitalWrite(LED_YELLOW, LOW);
  digitalWrite(LED_RED,    LOW);
}

// ── Setup ─────────────────────────────────────────────────────
void setup() {
  Serial.begin(115200);
  delay(200);
  Serial.println("\n=====================================");
  Serial.println("  Telemetria de Empuxo v2.5");
  Serial.println("=====================================");

  pinMode(LED_YELLOW, OUTPUT);
  pinMode(LED_RED,    OUTPUT);
  digitalWrite(LED_YELLOW, LOW);
  digitalWrite(LED_RED,    LOW);
  bootSequence();

  loadServerIP();

  // Detecta botao BOOT para forcar portal
  pinMode(BOOT_BTN, INPUT_PULLUP);
  delay(100);
  bool forcePortal = (digitalRead(BOOT_BTN) == LOW) || FORCE_PORTAL;

  if (forcePortal) {
    Serial.println("[WiFi] BOOT detectado — apagando credenciais...");
    WiFiManager wmTemp;
    wmTemp.resetSettings();
    delay(300);
  }

  // ── WiFiManager ────────────────────────────────────────────
  WiFiManager wm;

  WiFiManagerParameter paramIP("server_ip", "IP do Servidor Python", serverIP, 40);
  paramIP_ptr = &paramIP;
  wm.addParameter(&paramIP);
  wm.setSaveParamsCallback(saveParamCallback);

  wm.setConfigPortalTimeout(0);
  wm.setBreakAfterConfig(false);
  wm.setScanDispPerc(true);
  wm.setMinimumSignalQuality(10);
  wm.setShowInfoErase(false);

  wm.setTitle("Telemetria Foguete");
  wm.setCustomHeadElement(
    "<style>"
    "body{font-family:Arial,sans-serif;background:#0d0d0d;color:#eee;}"
    "h1,h2{color:#e53935;}"
    "input[type=text],input[type=password]{"
      "background:#1c1c1c;color:#fff;"
      "border:1px solid #444;padding:10px;"
      "width:100%;margin-bottom:8px;}"
    "input[type=submit],button{"
      "background:#e53935;color:#fff;"
      "border:none;padding:12px;"
      "font-size:15px;width:100%;cursor:pointer;margin-top:8px;}"
    "label{color:#aaa;font-size:13px;}"
    "</style>"
  );

  Serial.printf("[WiFi] AP: %s  Senha: %s\n", AP_NAME, AP_PASS);
  Serial.println("[WiFi] Apos conectar ao AP, acesse: http://192.168.4.1");

  bool connected;
  if (forcePortal) {
    Serial.println("[WiFi] Abrindo portal...");
    connected = wm.startConfigPortal(AP_NAME, AP_PASS);
  } else {
    Serial.println("[WiFi] Tentando reconectar...");
    connected = wm.autoConnect(AP_NAME, AP_PASS);
  }

  if (!connected) {
    Serial.println("[WiFi] Falha. Reiniciando em 3s...");
    delay(3000);
    ESP.restart();
  }

  // Recarrega IP (pode ter sido atualizado no portal)
  loadServerIP();

  Serial.println("\n[WiFi] Conectado!");
  Serial.printf("[WiFi] IP ESP32   : %s\n", WiFi.localIP().toString().c_str());
  Serial.printf("[WiFi] IP Servidor: %s:%d\n", serverIP, SERVER_PORT);
  Serial.printf("[WiFi] Dashboard  : http://%s:8080/dashboard.html\n", serverIP);
  Serial.println("=====================================\n");

  // ── HX711 ─────────────────────────────────────────────────
  scale.begin(DOUT_PIN, SCK_PIN);
  scale.set_scale(CAL_FACTOR);

  Serial.println("[HX711] Aguardando estabilizar...");
  for (int i = 0; i < 10; i++) {
    while (!scale.is_ready()) delay(10);
    scale.get_units(1);
  }
  scale.tare(10);
  Serial.println("[HX711] Tara OK. Pronto para medir.");

  // ── WebSocket ─────────────────────────────────────────────
  wsClient.begin(serverIP, SERVER_PORT, "/");
  wsClient.onEvent(onWsEvent);
  wsClient.setReconnectInterval(RECONNECT_MS);
  wsClient.enableHeartbeat(0, 0, 0);

  Serial.printf("[WS] Conectando a %s:%d...\n", serverIP, SERVER_PORT);
}

// ── Loop ──────────────────────────────────────────────────────
void loop() {
  wsClient.loop();

  // Maquina de estados dos LEDs — nao bloqueia
  tickLEDs();

  // Monitora WiFi e reconecta se necessario
  unsigned long now = millis();
  if (now - lastWifiChk > WIFI_RECONNECT_S) {
    lastWifiChk = now;
    if (WiFi.status() != WL_CONNECTED) {
      Serial.println("[WiFi] Conexao perdida. Reconectando...");
      WiFi.reconnect();
    }
  }

  // Throttle 50 Hz
  if (now - lastSend < SEND_INTERVAL_MS) return;
  lastSend = now;

  if (!scale.is_ready()) return;

  // Leitura em kgf → converte para N
  float kgf     = scale.get_units(1);
  float newtons = kgf * 9.80665f;

  // Pulso de heartbeat — indica que HX711 esta lendo
  triggerHeartbeat();

  // Filtro EMA (suavizacao exponencial)
  filtered = EMA_ALPHA * newtons + (1.0f - EMA_ALPHA) * filtered;

  // Dead-band: zera ruidos de fundo
  float output = (fabsf(filtered) < NOISE_FLOOR) ? 0.0f : filtered;

  // Nao envia se WebSocket nao estiver conectado
  if (!wsConnected) return;

  // Monta JSON compacto e envia
  StaticJsonDocument<96> doc;
  doc["t"] = millis();
  doc["f"] = round(output  * 100.0f) / 100.0f;
  doc["r"] = round(newtons * 100.0f) / 100.0f;

  String payload;
  payload.reserve(64);
  serializeJson(doc, payload);
  wsClient.sendTXT(payload);
}
