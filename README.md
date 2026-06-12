# 🚀 TeleEmpuxo — Sistema de Telemetria de Empuxo

**Versão:** ESP32 v2.5 · App Unificado v5.0  
**Objetivo:** Medir, registrar e visualizar em tempo real a força de empuxo de motores de foguete usando uma célula de carga HX711 e um ESP32, com transmissão via WebSocket e interface desktop nativa.

---

## Índice

1. [Visão geral do sistema](#1-visão-geral-do-sistema)
2. [O que mudou na v5.0](#2-o-que-mudou-na-v50)
3. [Requisitos](#3-requisitos)
4. [Configuração do hardware](#4-configuração-do-hardware)
5. [Configuração do ESP32](#5-configuração-do-esp32)
6. [Instalação e execução](#6-instalação-e-execução)
7. [A interface unificada](#7-a-interface-unificada)
8. [Overlays para OBS / Transmissão](#8-overlays-para-obs--transmissão)
9. [Como conduzir um teste](#9-como-conduzir-um-teste)
10. [Comportamento do estado COMPLETE](#10-comportamento-do-estado-complete)
11. [Lendo e interpretando os dados](#11-lendo-e-interpretando-os-dados)
12. [Máquina de missão (countdown)](#12-máquina-de-missão-countdown)
13. [Compensação de drift / creep](#13-compensação-de-drift--creep)
14. [LEDs do ESP32](#14-leds-do-esp32)
15. [Arquivos gerados](#15-arquivos-gerados)
16. [Solução de problemas](#16-solução-de-problemas)
17. [Parâmetros técnicos](#17-parâmetros-técnicos)

---

## 1. Visão geral do sistema

```
[Célula de Carga]
      │
   [HX711]
      │
   [ESP32] ──WiFi──► [telemetria_app.py]  ← interface desktop unificada
                          │
                          ├──► [overlay.html]      ← HUD leve para OBS
                          ├──► [overlay_mini.html] ← versão compacta para OBS
                          │
                     [logs/*.csv]   ← dados brutos
                     [plots/*.png]  ← gráficos automáticos
```

O ESP32 lê a célula de carga a **50 Hz**, converte os valores para Newtons e envia via WebSocket ao app Python. O app distribui os dados para os overlays em tempo real, detecta automaticamente o início e fim da queima, integra o impulso, compensa drift da célula e salva tudo em CSV.

---

## 2. O que mudou na v5.0

A v5.0 unifica em **um único arquivo Python** (`telemetria_app.py`) o que antes eram três arquivos separados:

| Antes (v4.x) | Agora (v5.0) |
|---|---|
| `server.py` | ✅ Integrado ao `telemetria_app.py` |
| `dashboard.html` | ✅ Integrado ao `telemetria_app.py` (janela nativa) |
| `layer_control.html` | ✅ Integrado ao `telemetria_app.py` (aba Missão) |
| `overlay.html` | Mantido — usado no OBS via HTTP |
| `overlay_mini.html` | Novo — versão compacta para OBS |

**Outras mudanças relevantes:**

- **Compensação de drift em software** — o app estima e corrige o drift da célula de carga por regressão linear, sem precisar reenviar tara ao ESP32.
- **Controle manual do estado COMPLETE** — a sessão só transita para `COMPLETE` ao clicar em **Parar**. Anteriormente, o sistema encerrava automaticamente quando detectava o fim da queima.

---

## 3. Requisitos

### Hardware

| Componente | Observação |
|---|---|
| ESP32 (qualquer variante com WiFi) | Testado com ESP32-WROOM-32 |
| Módulo HX711 | Qualquer breakout padrão |
| Célula de carga compatível | Capacidade recomendada: 5–50 kg |
| LED amarelo + resistor 220Ω | Pino 18 |
| LED vermelho + resistor 220Ω | Pino 19 |
| LED verde (opcional) | Ligado direto no VCC — indica alimentação |

### Software — ESP32

- Arduino IDE 2.x ou PlatformIO
- Bibliotecas (instalar via Library Manager):
  - `HX711 by Bogdan Necula`
  - `WebSockets by Markus Sattler`
  - `WiFiManager by tzapu`
  - `ArduinoJson by Benoit Blanchon`

### Software — App Python

- Python 3.10+
- Bibliotecas:

```bash
pip install customtkinter matplotlib websockets aiohttp numpy
```

---

## 4. Configuração do hardware

### Pinagem ESP32

| Pino ESP32 | Função |
|---|---|
| GPIO 15 | HX711 DOUT (dados) |
| GPIO 5 | HX711 SCK (clock) |
| GPIO 18 | LED amarelo (status) |
| GPIO 19 | LED vermelho (queima/perigo) |
| GPIO 0 | Botão BOOT (forçar portal WiFi) |
| VCC / GND | LED verde (alimentação sempre acesa) |

### Diagrama simplificado

```
ESP32 GPIO15 ──► HX711 DOUT
ESP32 GPIO5  ──► HX711 SCK
HX711 VDD    ──► 3.3V
HX711 GND    ──► GND
HX711 E+/E-  ──► Célula de carga (excitação)
HX711 A+/A-  ──► Célula de carga (sinal)
```

---

## 5. Configuração do ESP32

### Antes de compilar

Abra `esp32_telemetria.ino` e ajuste estas duas linhas:

```cpp
// ⚠️ OBRIGATÓRIO: mude para false antes de usar em campo
#define FORCE_PORTAL  false

// Ajuste conforme sua calibração (veja seção de calibração abaixo)
#define CAL_FACTOR   -2280.0f
```

> **Por que FORCE_PORTAL?** Quando `true`, o ESP32 apaga as credenciais WiFi salvas a cada reinicialização e abre o portal de configuração. Deixe `true` apenas durante a configuração inicial. Em campo, deixe `false`.

### Calibração do CAL_FACTOR

1. Compile e grave com `FORCE_PORTAL true`
2. Conecte ao WiFi e deixe o ESP32 rodando
3. Coloque um peso conhecido (ex: 500 g) na célula de carga
4. Abra o Serial Monitor (115200 baud) e observe o valor bruto
5. Ajuste `CAL_FACTOR` até que o valor lido corresponda ao peso em kgf
6. O sinal negativo (`-2280.0f`) indica que a célula está invertida — normal

### Configuração WiFi (primeiro uso)

1. Grave o firmware com `FORCE_PORTAL true`
2. Ligue o ESP32 — ele criará uma rede chamada **`Telemetria-Config`** (senha: `12345678`)
3. Conecte seu celular ou notebook nessa rede
4. Acesse **http://192.168.4.1** no navegador
5. Clique em **"Configurar WiFi"**
6. Escolha sua rede, insira a senha
7. Preencha o campo **"IP do Servidor Python"** com o IP da máquina onde o app rodará (ex: `192.168.1.100`)
8. Clique **Salvar** — o ESP32 reinicia e conecta automaticamente

> **Como saber o IP?** No Windows: `ipconfig`. No Linux/Mac: `ip a` ou `ifconfig`. Use o IP da interface WiFi/Ethernet conectada à mesma rede.

### Trocando de rede depois

Segure o botão **BOOT** ao ligar o ESP32. O portal abrirá novamente.

---

## 6. Instalação e execução

```bash
# 1. Instalar dependências Python
pip install customtkinter matplotlib websockets aiohttp numpy

# 2. Colocar os arquivos na mesma pasta:
#    telemetria_app.py
#    overlay.html
#    overlay_mini.html

# 3. Executar
python telemetria_app.py
```

O terminal mostrará:

```
==========================================================
  TELEMETRIA DE EMPUXO — App Unificado  v5.0
==========================================================
  WebSocket ESP32   : ws://0.0.0.0:8765
  WebSocket Clientes: ws://0.0.0.0:8766
  Overlay OBS       : http://localhost:8080/overlay.html
  API Health        : http://localhost:8080/api/health
==========================================================
```

A janela do app abrirá automaticamente. Não é mais necessário abrir nenhum navegador para operar — apenas para o OBS.

---

## 7. A interface unificada

O app é organizado em abas na janela principal:

### Aba Principal — Monitoramento

```
┌──────────────────────────────────────────────────────┐
│  ● WS  ● ESP32  [ID SESSÃO]              v5.0        │
├──────────────────────────────────────────────────────┤
│                                                      │
│              GRÁFICO DE EMPUXO                       │
│              (tempo real, 50 Hz)                     │
│                                                      │
├───────────────────────────────────────────┬──────────┤
│  FORÇA  │ PICO │ IMPULSO │ DURAÇÃO │ STATE│  LOG DE  │
├───────────────────────────────────────────┤  EVENTOS │
│  [▶ INICIAR] [■ PARAR] [📊 PLOT] [⚖ TARE] [↺ RESET] │
└──────────────────────────────────────────────────────┘
```

**Métricas exibidas:**

| Métrica | Descrição |
|---|---|
| **FORÇA** | Empuxo instantâneo em Newtons (50 Hz) |
| **PICO** | Maior força registrada na sessão atual |
| **IMPULSO** | Área sob a curva de empuxo (N·s) |
| **DURAÇÃO** | Tempo de queima detectado (s) |
| **STATE** | Estado atual: IDLE / RUNNING / COMPLETE |

**Botões:**

| Botão | Função |
|---|---|
| **INICIAR** | Inicia uma nova sessão de medição |
| **PARAR** | Encerra a sessão e transita para COMPLETE |
| **PLOT** | Gera o gráfico PNG da sessão atual |
| **TARE** | Zera a balança (faça com célula vazia) |
| **RESET** | Limpa os dados e volta para IDLE |

### Aba Missão — Controle de Countdown

Contém o sistema GO/NO-GO e a contagem regressiva. Veja a [seção 12](#12-máquina-de-missão-countdown) para detalhes.

---

## 8. Overlays para OBS / Transmissão

O app serve os overlays via HTTP na porta 8080. Use-os como **Fonte de Navegador** no OBS:

### `overlay.html` — Overlay completo

Exibe empuxo em tempo real, arco de progresso, histórico em gráfico e banner de resultado ao final.

```
http://<IP>:8080/overlay.html?ip=<IP>&port=8766
```

### `overlay_mini.html` — Overlay compacto (canto inferior esquerdo)

HUD minimalista com fundo transparente — ideal para sobrepor ao vídeo sem ocupar muito espaço. Exibe:

- Força de empuxo em tempo real (número grande)
- Barra de progresso proporcional ao `MAX_FORCE`
- Pico (N) · Tempo de queima (s) · Impulso (N·s)
- Card de resultado ao fim da sessão (some após 30 s)
- Pill de status de conexão

```
http://<IP>:8080/overlay_mini.html?ip=<IP>&port=8766&max=120
```

**Parâmetros de URL:**

| Parâmetro | Padrão | Descrição |
|---|---|---|
| `ip` | hostname atual | IP do servidor |
| `port` | `8766` | Porta WebSocket dos clientes |
| `max` | `120` | Força máxima para a barra de progresso (N) |

> **Dica OBS:** Ative "Controlar áudio via OBS" desativado e marque "Transparência" na fonte de navegador para que o fundo transparente funcione corretamente.

---

## 9. Como conduzir um teste

### Procedimento padrão (sem countdown)

```
1. Executar telemetria_app.py
      └─► Aguardar janela abrir

2. Ligar o ESP32
      └─► Aguardar LED amarelo piscar lento (conectado)
      └─► Dot ESP32 fica verde no app

3. Fazer a tara
      └─► Célula de carga vazia, sem o motor
      └─► Clicar TARE e aguardar log "Tara aplicada"

4. Prender o motor na célula de carga

5. Afastar todos da área de risco

6. Clicar INICIAR → state muda para RUNNING

7. Acionar a ignição

8. O sistema detecta a queima automaticamente (força > 2.0 N)

9. Aguardar o fim da queima
      └─► Quando a força cair abaixo de 2.0 N por 1.5 s:
      └─► "burn_end" é registrado no log
      └─► LED do ESP32 entra em cooldown (vermelho piscando)
      └─► A sessão PERMANECE em RUNNING

10. Clicar PARAR → state muda para COMPLETE
      └─► CSV é fechado
      └─► Gráfico PNG é gerado automaticamente

11. Clicar RESET para preparar o próximo teste
```

> **Importante:** A sessão não encerra sozinha após a queima. O operador precisa clicar **PARAR** para finalizar — isso evita que dados pós-queima sejam perdidos e garante controle explícito sobre o encerramento.

### Procedimento com countdown

```
1. Abrir a aba Missão no app

2. Marcar todos os itens como GO no checklist
      └─► ESP32 é marcado automaticamente quando conectado
      └─► Câmera, HX711, Área Livre e Clima são manuais

3. Configurar o tempo de contagem (ex: 60 s)

4. Clicar START MISSION
      └─► Contagem só inicia se todos estiverem GO

5. Acompanhar o countdown
      └─► HOLD: pausa (útil para resolver imprevistos)
      └─► RESUME: retoma de onde parou
      └─► SCRUB: aborta a missão

6. Em T-0, o sistema aguarda o empuxo
      └─► Assim que força > 2.0 N, sessão inicia automaticamente

7. Resto do procedimento igual ao padrão (passos 8–11 acima)
```

---

## 10. Comportamento do estado COMPLETE

A transição de estados da sessão funciona assim:

```
IDLE ──[INICIAR]──► RUNNING ──[PARAR]──► COMPLETE ──[RESET]──► IDLE
                       │
                  [queima detectada]
                  [queima termina]
                       │
                  permanece em RUNNING
                  até o operador clicar PARAR
```

**Antes da v5.0**, o sistema encerrava a sessão automaticamente ao detectar o fim da queima (`burn_end`). Isso causava problemas em testes com queima rápida, pois o operador perdia a chance de observar a curva completa antes do encerramento.

**A partir da v5.0**, o `burn_end` apenas registra o evento e ativa o cooldown no ESP32 — a sessão permanece `RUNNING`. O estado só vai para `COMPLETE` quando o operador clicar **PARAR** (ou quando a missão for abortada via SCRUB).

---

## 11. Lendo e interpretando os dados

### O que é cada dado

| Dado | Unidade | Descrição |
|---|---|---|
| `force_N` | Newtons (N) | Empuxo filtrado (EMA α=0.3 + compensação de drift) |
| `force_raw_N` | Newtons (N) | Leitura bruta do HX711, sem compensação extra |
| `elapsed_s` | Segundos | Tempo desde o início da queima detectada |
| `timestamp_ms` | Milissegundos | Tempo do ESP32 desde o boot (millis()) |

### Métricas calculadas

**Empuxo de pico (N)**  
Maior valor instantâneo registrado. Representa o máximo que o motor pode entregar por um instante. Não use para calcular levantamento.

**Empuxo médio (N)**  
Média de todas as amostras durante a queima. Este é o número correto para calcular capacidade de levantamento.

**Impulso total (N·s)**  
Integral da força ao longo do tempo (regra dos trapézios). Usada para classificar motores no sistema NAR/NARAM (A, B, C… cada letra dobra o impulso).

**Duração da queima (s)**  
Intervalo entre o primeiro e o último momento com empuxo acima de 2.0 N.

### Capacidade de levantamento

```
Empuxo médio (N) ÷ 9.80665 = capacidade em kgf

Exemplo: 10.64 N ÷ 9.80665 ≈ 1.08 kgf
```

O foguete precisa pesar **menos** que esse valor para subir. Para voo estável recomenda-se que o foguete pese no máximo **1/5** do empuxo médio em kgf.

### Lendo o CSV

O arquivo `logs/test_AAAAMMDD_HHMMSS.csv` tem quatro colunas:

```csv
timestamp_ms, elapsed_s, force_N, force_raw_N
622839,       0.0000,    -1.27,   -1.29    ← pré-queima (ruído)
...
630000,       1.2000,    15.43,   15.81    ← durante queima
...
```

- Linhas com `elapsed_s = 0.0000` são amostras pré-queima (sessão em RUNNING, queima ainda não detectada)
- Valores negativos pequenos (~−1 N) indicam tara levemente descalibrada
- A queima real começa quando `force_N` cruza 2.0 N

### Lendo o gráfico PNG

O gráfico gerado em `plots/plot_AAAAMMDD_HHMMSS.png` tem dois painéis:

**Painel superior — Curva de empuxo**
- Área vermelha escura = empuxo ao longo do tempo
- Área laranja mais clara = período de queima ativa
- Linha tracejada amarela = pico de empuxo
- Linha tracejada cinza = threshold de queima (2.0 N)
- Caixa de texto = resumo: impulso, força média, pico e duração

**Painel inferior — Taxa de variação (dF/dt)**
- Velocidade de mudança da força em N/s
- Pico positivo = aceleração do motor
- Pico negativo = extinção / desaceleração
- Útil para identificar instabilidades na combustão

---

## 12. Máquina de missão (countdown)

```
IDLE ──► COUNTING ──► (T-0) ──► aguarda empuxo ──► [sessão inicia auto]
           │
           ▼
          HOLD ──► COUNTING (resume)
           │
           ▼
         SCRUB
```

**Estados:**

| Estado | Significado |
|---|---|
| `mission_idle` | Aguardando início da contagem |
| `mission_counting` | Contagem regressiva ativa |
| `mission_hold` | Contagem pausada |
| `mission_scrubbed` | Missão abortada |

**Checklist GO/NO-GO:**

| Item | Tipo | O que verificar |
|---|---|---|
| ESP32 | Automático | Hardware conectado ao app |
| Câmera | Manual | Posicionada e gravando |
| HX711 / Balança | Manual | Célula montada e tarada |
| Área Livre | Manual | Ninguém no raio de segurança |
| Condição Climática | Manual | Vento e umidade aceitáveis |

**Regras importantes:**
- A contagem só inicia se **todos** os itens estiverem GO
- Se a missão for Scrubbed com sessão ativa, a sessão é encerrada imediatamente
- Após T-0, o sistema aguarda o empuxo — se o motor não acionar, execute SCRUB manual

---

## 13. Compensação de drift / creep

A v5.0 implementa compensação automática de drift da célula de carga em software, sem precisar retarar o ESP32:

**Como funciona:**
1. O sistema coleta amostras enquanto `state == IDLE` (sem queima)
2. Mantém uma janela deslizante de 50 segundos
3. Aplica regressão linear para estimar a taxa de drift (N/s) e o offset atual
4. Subtrai o offset de cada leitura antes de processar

**Parâmetros relevantes:**

| Parâmetro | Valor padrão | Descrição |
|---|---|---|
| `DRIFT_WINDOW_S` | 50 s | Janela de amostras para regressão |
| `DRIFT_MIN_SAMPLES` | 15 | Mínimo de amostras para estimar |
| `DRIFT_APPLY_THRESH_N` | 0.20 N | Offset mínimo para aplicar compensação |
| `DRIFT_RATE_WARN_NS` | 0.05 N/s | Taxa que gera aviso no log |
| `DRIFT_HARD_TARE_N` | 0.5 N | Offset acumulado que força uma tara física |

Quando o offset acumulado ultrapassa `DRIFT_HARD_TARE_N`, o app envia uma tara física ao ESP32 e zera o modelo — isso aparece como aviso no log de eventos.

---

## 14. LEDs do ESP32

| LED Amarelo | LED Vermelho | Significado |
|---|---|---|
| Pisca rápido | Apagado | Sem WiFi ou sem WebSocket |
| Pisca lento | Apagado | Conectado, aguardando sessão |
| Pulso breve (heartbeat) | Apagado | Sessão armada, lendo HX711 |
| Apagado | Aceso fixo | Queima detectada — **NÃO SE APROXIME** |
| Apagado | Pisca | Cooldown pós-queima (15 s) — motor ainda quente |
| Pisca rápido | Pisca rápido | Erro: WebSocket perdido durante sessão ativa |

> **Atenção:** O LED vermelho piscando (cooldown) significa que o motor pode estar quente. Aguarde os 15 segundos antes de se aproximar.

---

## 15. Arquivos gerados

```
projeto/
├── telemetria_app.py
├── overlay.html
├── overlay_mini.html
├── logs/
│   ├── test_20260525_203021.csv    ← dados brutos do teste
│   └── test_20260526_141500.csv
└── plots/
    ├── plot_20260525_203021.png    ← gráfico automático
    └── plot_20260526_141500.png
```

Os arquivos também são acessíveis via HTTP:

- CSV: `http://<IP>:8080/logs/test_AAAAMMDD_HHMMSS.csv`
- PNG: `http://<IP>:8080/plots/plot_AAAAMMDD_HHMMSS.png`

---

## 16. Solução de problemas

### ESP32 não conecta ao WiFi
- Verifique se `FORCE_PORTAL` está `false` após a configuração inicial
- Segure BOOT ao ligar para reabrir o portal
- Confirme que o IP do servidor está correto no portal

### Dot ESP32 não fica verde
- Verifique o Serial Monitor do ESP32 para confirmar conexão WiFi
- Confirme que o IP do servidor salvo no ESP32 está correto
- Tente reconfigurar via portal BOOT

### Leituras negativas em repouso (ex: −1.2 N)
- A tara foi feita com algo sobre a célula, ou a célula está sobrecarregada
- Clique TARE com a célula completamente livre de carga
- Se persistir, verifique a montagem mecânica

### Gráfico não gera após PARAR
- O gráfico é gerado automaticamente ao clicar PARAR (transição para COMPLETE)
- Verifique se há permissão de escrita na pasta `plots/`
- Certifique-se de que há pelo menos 5 amostras registradas na sessão

### Valor de força oscila muito
- Ajuste `EMA_ALPHA` no firmware para um valor menor (mais suavização), ex: `0.08`
- Verifique se há vibração mecânica no suporte da célula
- Certifique-se de que o cabo do HX711 não está perto de fontes de interferência

### App fecha ao minimizar no Windows
- O Windows pode pausar processos com QuickEdit Mode ativo no terminal
- Se o app travar, clique na janela do terminal e pressione Enter

### Overlay não aparece no OBS
- Confirme que o `telemetria_app.py` está rodando
- Use `http://localhost:8080/overlay_mini.html` se estiver no mesmo computador
- Verifique se a porta 8080 está liberada no firewall

---

## 17. Parâmetros técnicos

### ESP32 — `esp32_telemetria.ino`

| Parâmetro | Valor padrão | Descrição |
|---|---|---|
| `CAL_FACTOR` | −2280.0 | Fator de calibração da célula de carga |
| `EMA_ALPHA` | 0.15 | Suavização exponencial no firmware |
| `NOISE_FLOOR` | 0.25 N | Leituras abaixo desse valor são zeradas |
| `SEND_INTERVAL_MS` | 20 ms | Taxa de envio = 50 Hz |
| `SERVER_PORT` | 8765 | Porta WebSocket do servidor |
| `LED_COOLDOWN_MS` | 15000 ms | Tempo de cooldown pós-queima |

### App Python — `telemetria_app.py`

| Parâmetro | Valor padrão | Descrição |
|---|---|---|
| `BURN_THRESHOLD` | 2.0 N | Limiar de detecção de queima |
| `BURN_END_DELAY` | 1.5 s | Tempo abaixo do limiar para confirmar fim da queima |
| `MAX_FORCE` | 120.0 N | Escala máxima dos gráficos PNG |
| `ESP32_PORT` | 8765 | Porta WebSocket para o ESP32 |
| `CLIENT_PORT` | 8766 | Porta WebSocket para overlays e clientes externos |
| `HTTP_PORT` | 8080 | Porta HTTP para overlays e download de arquivos |
| `ESP_HEARTBEAT_TIMEOUT` | 5.0 s | Tempo sem mensagem para marcar ESP como desconectado |
| `GRAPH_POINTS` | 500 | Número máximo de pontos no gráfico em tempo real |

### Portas utilizadas

| Porta | Protocolo | Uso |
|---|---|---|
| 8765 | WebSocket | Comunicação exclusiva com o ESP32 |
| 8766 | WebSocket | Broadcast para overlays e clientes externos |
| 8080 | HTTP | Overlays, API health e download de logs/plots |

---

*Documentação v5.0 — junho de 2026.*
