#!/usr/bin/env python3
# Crypto Signal Bot - GitHub Actions Compatible
import ccxt, pandas as pd, numpy as np, time, os, requests
from datetime import datetime

class Config:
    TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '')
    TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', '')
    SYMBOLS = ['BTC/USDT', 'ETH/USDT', 'SOL/USDT']
    SIGNAL_COOLDOWN = 3600

class CryptoEngine:
    def __init__(self):
        print("\n🚀 CRYPTO ENGINE SIGNAL BOT")
        print(f"Exchange: Kraken (GitHub Actions compatible)")
        self.exchange = ccxt.kraken({'enableRateLimit': True})
        self.last_signals = {}
        print("✅ Exchange initialized: kraken")

    def calculate_atr(self, df, period):
        h, l, c = df['high'], df['low'], df['close']
        tr = pd.concat([h-l, abs(h-c.shift()), abs(l-c.shift())], axis=1).max(axis=1)
        return tr.rolling(period).mean()

    def detect_regime(self, df):
        atr_s = self.calculate_atr(df, 14)
        atr_l = self.calculate_atr(df, 100)
        ratio = atr_s.iloc[-1] / atr_l.iloc[-1]
        gain_3d = ((df['close'].iloc[-1] - df['close'].iloc[-72]) / df['close'].iloc[-72]) * 100 if len(df) > 72 else 0
        trading = ratio > 1.3 and not (gain_3d > 30)
        return {'state': 'EXPANSION' if trading else 'CONTRACTION', 'atr_ratio': ratio, 'atr_short': atr_s.iloc[-1], 'gain_3d': gain_3d, 'trading_allowed': trading}

    def analyze_trend(self, df):
        ema50 = df['close'].ewm(span=50).mean()
        ema200 = df['close'].ewm(span=200).mean()
        if df['close'].iloc[-1] > ema50.iloc[-1] > ema200.iloc[-1]:
            return 'BULLISH'
        return 'NEUTRAL'

    def detect_entry(self, df, trend):
        ema50 = df['close'].ewm(span=50).mean()
        bounce = (df['low'].iloc[-1] <= ema50.iloc[-1] and 
                 df['close'].iloc[-1] > ema50.iloc[-1] and 
                 df['close'].iloc[-1] > df['open'].iloc[-1])
        quality = 2 if trend == 'BULLISH' else 0
        quality += 1 if df['close'].iloc[-1] > ema50.iloc[-1] else 0
        quality += 1 if df['volume'].iloc[-1] > df['volume'].rolling(20).mean().iloc[-1] * 1.2 else 0
        if bounce and trend == 'BULLISH' and quality >= 3:
            return {'signal': 'LONG', 'pattern': 'EMA Bounce', 'quality': quality, 'confidence': 'HIGH' if quality >= 4 else 'MEDIUM'}
        return None

    def calculate_levels(self, symbol, entry, atr):
        is_btc = 'BTC' in symbol
        mult = 1.5 if is_btc else 2.0
        sl = entry - (atr * mult)
        dist = entry - sl
        return {
            'entry': entry, 'stop_loss': sl,
            'tp1': entry + dist * 1.5,
            'tp2': entry + dist * 2.5,
            'tp3': entry + dist * 4.0,
            'risk_pct': (dist / entry) * 100
        }

    def scan_symbol(self, symbol):
        try:
            print(f"\n📊 Scanning {symbol}:")
            if symbol in self.last_signals and (time.time() - self.last_signals[symbol]) < Config.SIGNAL_COOLDOWN:
                print(f"  ⏸️ Cooldown active (last signal {int((time.time() - self.last_signals[symbol])/60)} min ago)")
                return None
            
            print(f"  📊 Fetching data for {symbol}...")
            htf = pd.DataFrame(self.exchange.fetch_ohlcv(symbol, '4h', 200), columns=['t', 'o', 'h', 'l', 'c', 'v']).rename(columns={'o':'open','h':'high','l':'low','c':'close','v':'volume'})
            ltf = pd.DataFrame(self.exchange.fetch_ohlcv(symbol, '1h', 200), columns=['t', 'o', 'h', 'l', 'c', 'v']).rename(columns={'o':'open','h':'high','l':'low','c':'close','v':'volume'})
            print(f"  ✅ Data fetched: {len(ltf)} candles")
            
            regime = self.detect_regime(ltf)
            if not regime['trading_allowed']:
                print(f"  ⏸️ Regime: {regime['state']} - no trading")
                return None
            
            trend = self.analyze_trend(htf)
            entry_data = self.detect_entry(ltf, trend)
            if not entry_data:
                print(f"  ⏸️ No entry signal (Regime: {regime['state']}, Trend: {trend})")
                return None
            
            entry_price = ltf['close'].iloc[-1]
            levels = self.calculate_levels(symbol, entry_price, regime['atr_short'])
            self.last_signals[symbol] = time.time()
            print(f"  🎯 SIGNAL FOUND! {entry_data['signal']} - {entry_data['confidence']}")
            
            return {
                'symbol': symbol, 'signal': entry_data['signal'], 'pattern': entry_data['pattern'],
                'quality': entry_data['quality'], 'confidence': entry_data['confidence'],
                'regime': regime, 'htf_trend': trend, 'levels': levels, 'timestamp': datetime.now()
            }
        except Exception as e:
            print(f"  ❌ Error {symbol}: {e}")
            return None

class TelegramNotifier:
    def __init__(self):
        self.bot_token = Config.TELEGRAM_BOT_TOKEN
        self.chat_id = Config.TELEGRAM_CHAT_ID
        self.base_url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        print("✅ Telegram bot initialized")

    def send_message(self, text):
        try:
            data = {'chat_id': self.chat_id, 'text': text, 'parse_mode': 'Markdown'}
            response = requests.post(self.base_url, data=data, timeout=10)
            if response.status_code == 200:
                print("✅ Message sent successfully")
                return True
            else:
                print(f"❌ Failed to send message: {response.status_code} - {response.text}")
                return False
        except Exception as e:
            print(f"❌ Telegram error: {e}")
            return False

    def format_signal(self, s):
        l = s['levels']
        return f'''🟢 *LONG SIGNAL* 🟢

💎 *Pair:* {s['symbol']}
📊 *Pattern:* {s['pattern']}
⭐ *Confidence:* {s['confidence']} ({s['quality']}/5)

🎯 *Entry:* `{l['entry']:.2f}`
🛡 *Stop Loss:* `{l['stop_loss']:.2f}`
   *Risk:* {l['risk_pct']:.2f}%

💰 *Take Profits:*
   TP1: `{l['tp1']:.2f}` (1.5R)
   TP2: `{l['tp2']:.2f}` (2.5R)
   TP3: `{l['tp3']:.2f}` (4.0R)

📈 *CONDITIONS*
Regime: {s['regime']['state']}
HTF Trend: {s['htf_trend']}
ATR Ratio: {s['regime']['atr_ratio']:.2f}
Time: {s['timestamp'].strftime('%H:%M:%S')}'''

    def send_signal(self, signal):
        msg = self.format_signal(signal)
        return self.send_message(msg)

def main():
    print("\n" + "="*50)
    print("  CRYPTO SIGNAL BOT - GITHUB ACTIONS")
    print("="*50)
    print(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Monitoring: {', '.join(Config.SYMBOLS)}")
    print("="*50)
    
    engine = CryptoEngine()
    notifier = TelegramNotifier()
    
    # Send startup message
    startup_msg = f'''🤖 *Bot Scan Started*

Exchange: Kraken
Pairs: {', '.join(Config.SYMBOLS)}
Time: {datetime.now().strftime('%H:%M:%S')} UTC

Scanning for signals...'''
    notifier.send_message(startup_msg)
    
    # Scan all symbols
    signals_found = []
    for symbol in Config.SYMBOLS:
        signal = engine.scan_symbol(symbol)
        if signal:
            signals_found.append(signal)
            notifier.send_signal(signal)
            time.sleep(2)
    
    # Send completion message
    if signals_found:
        completion_msg = f"✅ Scan complete: {len(signals_found)} signal(s) sent"
    else:
        completion_msg = "✅ Scan complete: No signals (markets in CONTRACTION or no setups)"
    
    print(f"\n{completion_msg}")
    notifier.send_message(completion_msg)
    print("\n" + "="*50)
    print("  BOT SCAN FINISHED")
    print("="*50)

if __name__ == "__main__":
    main()
