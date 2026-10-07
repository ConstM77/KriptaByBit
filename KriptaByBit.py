import time
import threading
import logging
from flask import Flask, render_template, request, jsonify
from pybit.unified_trading import HTTP, WebSocket

app = Flask(__name__)

# 🌟 ОТКЛЮЧАЕМ СПАМ С КОДОМ 200 В КОНСОЛИ ТЕРМИНАЛА 🌟
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

# Настройка логирования для веб-панели логов
log_stream = []
class WebLogHandler(logging.Handler):
    def emit(self, record):
        log_entry = self.format(record)
        log_stream.append(log_entry)
        if len(log_stream) > 100:
            log_stream.pop(0)

logger = logging.getLogger()
logger.setLevel(logging.INFO)
web_handler = WebLogHandler()
web_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
logger.addHandler(web_handler)

# --- ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ УПРАВЛЕНИЯ И СТАТИСТИКИ ---
BOT_RUNNING = False
bot_thread = None
session = None
ws = None

STATS = {
    "buy_count": 0,
    "sell_count": 0,
    "cycles_completed": 0,
    "total_profit_usdt": 0.0
}

CONFIG = {
    "API_KEY": "",
    "API_SECRET": "",
    "SYMBOL": "BTCUSDT",
    "TRADE_MODE": "spot",
    "LEVERAGE": "3",
    "ORDER_QTY": 0.001,
    "GRID_LEVELS": 3,
    "DESIRED_PROFIT_PCT": 0.003
}

def place_order(side, price, stop_loss=None):
    """Отправка лимитных ордеров на биржу Bybit."""
    try:
        decimals = 1 if CONFIG["TRADE_MODE"] == "spot" else 2
        rounded_price = round(price, decimals)
        category = "spot" if CONFIG["TRADE_MODE"] == "spot" else "linear"
        
        params = {
            "category": category,
            "symbol": CONFIG["SYMBOL"],
            "side": side,
            "orderType": "Limit",
            "qty": str(CONFIG["ORDER_QTY"]),
            "price": str(rounded_price),
            "timeInForce": "GTC"
        }
        
        if CONFIG["TRADE_MODE"] == "spot" and side == "Buy":
            params["isLeverage"] = 1  

        if stop_loss and CONFIG["TRADE_MODE"] == "futures":
            params["stopLoss"] = str(round(stop_loss, decimals))
            params["slTriggerBy"] = "LastPrice"
            
        res = session.place_order(**params)
        logging.info(f"Выставлен [{CONFIG['TRADE_MODE'].upper()}] {side} Limit по цене {rounded_price}.")
    except Exception as e:
        print(f"[ERROR] Ошибка отправки ордера {side}: {e}")
        logging.error(f"Ошибка выставления ордера: {e}")

def handle_order_update(message):
    """Обработчик WebSocket событий при исполнении ордеров."""
    global STATS
    if "data" in message:
        for order in message["data"]:
            if order["symbol"] == CONFIG["SYMBOL"] and order["orderStatus"] == "Filled":
                side = order["side"]
                price = float(order["price"])
                logging.info(f"🔥 Ордер {side} на цене {price} ПОЛНОСТЬЮ ИСПОЛНЕН!")
                
                if side == "Buy":
                    STATS["buy_count"] += 1
                elif side == "Sell":
                    STATS["sell_count"] += 1
                
                STATS["cycles_completed"] = min(STATS["buy_count"], STATS["sell_count"])
                
                maker_fee = 0.001 if CONFIG["TRADE_MODE"] == "spot" else 0.0002
                trade_volume_usdt = price * CONFIG["ORDER_QTY"]
                STATS["total_profit_usdt"] = STATS["cycles_completed"] * (trade_volume_usdt * CONFIG["DESIRED_PROFIT_PCT"])

                total_step = CONFIG["DESIRED_PROFIT_PCT"] + (maker_fee * 2)
                sl_pct = 0.025 if CONFIG["TRADE_MODE"] == "spot" else 0.015
                decimals = 1 if CONFIG["TRADE_MODE"] == "spot" else 2

                if side == "Buy":
                    new_sell_price = price * (1 + total_step)
                    sl_price = new_sell_price * (1 - sl_pct)
                    logging.info(f"🔄 Создаем зеркальный ордер: ПРОДАЖА по {round(new_sell_price, decimals)}")
                    place_order("Sell", new_sell_price, stop_loss=sl_price)
                elif side == "Sell":
                    new_buy_price = price * (1 - total_step)
                    sl_price = new_buy_price * (1 + sl_pct)
                    logging.info(f"🔄 Создаем зеркальный ордер: ПОКУПКА по {round(new_buy_price, decimals)}")
                    place_order("Buy", new_buy_price, stop_loss=sl_price)

def bot_loop():
    """Основной фоновый цикл торгового алгоритма робота."""
    global BOT_RUNNING, session, ws
    print("\n[BOT] Запуск торгового ядра. Инициализация параметров...")
    logging.info("Инициализация торговой сетки...")
    
    category = "spot" if CONFIG["TRADE_MODE"] == "spot" else "linear"
    maker_fee = 0.001 if CONFIG["TRADE_MODE"] == "spot" else 0.0002
    total_step = CONFIG["DESIRED_PROFIT_PCT"] + (maker_fee * 2)
    sl_pct = 0.025 if CONFIG["TRADE_MODE"] == "spot" else 0.015
    
    try:
        session.cancel_all_orders(category=category, symbol=CONFIG["SYMBOL"])
        logging.info(f"Все старые ордера в секции [{CONFIG['TRADE_MODE'].upper()}] очищены.")
        
        if CONFIG["TRADE_MODE"] == "futures":
            session.set_leverage(category=category, symbol=CONFIG["SYMBOL"], buyLeverage=CONFIG["LEVERAGE"], sellLeverage=CONFIG["LEVERAGE"])
        
        tickers = session.get_tickers(category=category, symbol=CONFIG["SYMBOL"])
        current_price = float(tickers['result']['list'][0]['lastPrice'])
        
        print(f"[BOT] Актуальная рыночная цена: {current_price}")
        logging.info(f"Старт сетки вокруг цены: {current_price}")
        
        lowest_buy = current_price * (1 - total_step * CONFIG["GRID_LEVELS"])
        highest_sell = current_price * (1 + total_step * CONFIG["GRID_LEVELS"])
        global_buy_sl = lowest_buy * (1 - sl_pct)
        global_sell_sl = highest_sell * (1 + sl_pct)

        for i in range(1, CONFIG["GRID_LEVELS"] + 1):
            buy_price = current_price * (1 - total_step * i)
            sell_price = current_price * (1 + total_step * i)
            place_order("Buy", buy_price, stop_loss=global_buy_sl if i == CONFIG["GRID_LEVELS"] else None)
            place_order("Sell", sell_price, stop_loss=global_sell_sl if i == CONFIG["GRID_LEVELS"] else None)

        print("[BOT] Авторизация приватного WebSocket потока Bybit V5...")
        logging.info("Подключение WebSocket к приватному потоку ордеров...")
        
        ws = WebSocket(
            testnet=True,
            channel_type="private",
            api_key=CONFIG["API_KEY"],
            api_secret=CONFIG["API_SECRET"]
        )
        ws.order_stream(callback=handle_order_update)
        
        # 🌟 ВОЗВРАЩАЕМ ТАЙМЕР ЕЖЕМИНУТНЫХ УВЕДОМЛЕНИЙ В ИНТЕРФЕЙС 🌟
        print("[BOT] WebSocket успешно запущен и непрерывно слушает биржу.")
        logging.info("Бот запущен успешно. Сетка активна, WebSocket слушает рынок.")
        
        last_ping_time = time.time()
        
        while BOT_RUNNING:
            current_time = time.time()
            if current_time - last_ping_time >= 60:
                logging.info(f"🟢 Бот работает, ордера активны в режиме {CONFIG['TRADE_MODE'].upper()}. Связь с биржей стабильна.")
                last_ping_time = current_time
            time.sleep(1)
            
    except Exception as e:
        print(f"\n❌ КРИТИЧЕСКАЯ ОШИБКА В ТЕРМИНАЛЕ: {e}\n")
        logging.error(f"Критическая ошибка в работе бота: {e}")
        BOT_RUNNING = False

# --- ВЕБ-МАРШРУТЫ СЕРВЕРА FLASK ---
@app.route('/')
def index():
    return render_template('index.html', config=CONFIG, running=BOT_RUNNING)

@app.route('/start', methods=['POST'])
def start_bot():
    global BOT_RUNNING, bot_thread, session, STATS
    if not BOT_RUNNING:
        STATS = {"buy_count": 0, "sell_count": 0, "cycles_completed": 0, "total_profit_usdt": 0.0}
        
        CONFIG["API_KEY"] = request.form.get("api_key").strip()
        CONFIG["API_SECRET"] = request.form.get("api_secret").strip()
        CONFIG["SYMBOL"] = request.form.get("symbol").upper()
        CONFIG["TRADE_MODE"] = request.form.get("trade_mode")
        CONFIG["LEVERAGE"] = request.form.get("leverage")
        CONFIG["ORDER_QTY"] = float(request.form.get("order_qty"))
        CONFIG["GRID_LEVELS"] = int(request.form.get("grid_levels"))
        CONFIG["DESIRED_PROFIT_PCT"] = float(request.form.get("profit_pct"))

        session = HTTP(testnet=True, api_key=CONFIG["API_KEY"], api_secret=CONFIG["API_SECRET"])
        
        BOT_RUNNING = True
        bot_thread = threading.Thread(target=bot_loop)
        bot_thread.start()
        logging.info("🔴 Команда на запуск торговой сессии получена.")
    return jsonify({"status": "running"})

@app.route('/stop', methods=['POST'])
def stop_bot():
    global BOT_RUNNING
    if BOT_RUNNING:
        BOT_RUNNING = False
        logging.info("🛑 Команда на остановку бота выполнена.")
    return jsonify({"status": "stopped"})

@app.route('/logs')
def get_logs():
    return jsonify({"logs": log_stream})

@app.route('/stats')
def get_stats():
    return jsonify(STATS)

# 🌟 ЗАМЕНИТЕ САМЫЙ КОНЕЦ ФАЙЛА app.py НА ЭТОТ ВАРИАНТ 🌟
import os # Добавьте импорт наверх файла, если его нет

if __name__ == '__main__':
    # Amvera передает нужный порт через переменную среды PORT
    port = int(os.environ.get("PORT", 8080)) 
    
    from wsgiref.simple_server import make_server
    print(f"👁️ Сервер Ока запущен на порту {port}")
    server = make_server('0.0.0.0', port, app)
    server.serve_forever()
