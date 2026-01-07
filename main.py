#!/usr/bin/env python3
# Crypto Engine Telegram Signal Bot

import ccxt, pandas as pd, numpy as np, asyncio, time
from datetime import datetime
from telegram import Bot
from telegram.constants import ParseMode

class Config:
    TELEGRAM_BOT_TOKEN = "8579736001:AAHB91-875laeiEO3Qu3gkXhjuXA8VnPrJI"
    TELEGRAM_CHAT_ID = "9972466517"
    SYMBOLS = ['BTC/USDT', 'ETH/USDT', 'SOL/USDT']
    SCAN_INTERVAL = 300
    SIGNAL_COOLDOWN = 3600

class CryptoEngine:
    def __init__(self):
        self.exchange = ccxt.binance({'enableRateLimit': True})
        self.last_signals = {}

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

    async def scan_symbol(self, symbol):
        try:
            if symbol in self.last_signals and (time.time() - self.last_signals[symbol]) < Config.SIGNAL_COOLDOWN:
                return None

            htf = pd.DataFrame(self.exchange.fetch_ohlcv(symbol, '4h', 200), columns=['t', 'o', 'h', 'l', 'c', 'v']).rename(columns={'o':'open','h':'high','l':'low','c':'close','v':'volume'})
            ltf = pd.DataFrame(self.exchange.fetch_ohlcv(symbol, '1h', 200), columns=['t', 'o', 'h', 'l', 'c', 'v']).rename(columns={'o':'open','h':'high','l':'low','c':'close','v':'volume'})

            regime = self.detect_regime(ltf)
            if not regime['trading_allowed']:
                return None

            trend = self.analyze_trend(htf)
            entry_data = self.detect_entry(ltf, trend)

            if not entry_data:
                return None

            entry_price = ltf['close'].iloc[-1]
            levels = self.calculate_levels(symbol, entry_price, regime['atr_short'])

            self.last_signals[symbol] = time.time()

            return {
                'symbol': symbol, 'signal': entry_data['signal'], 'pattern': entry_data['pattern'],
                'quality': entry_data['quality'], 'confidence': entry_data['confidence'],
                'regime': regime, 'htf_trend': trend, 'levels': levels, 'timestamp': datetime.now()
            }
        except Exception as e:
            print(f"Error {symbol}: {e}")
            return None

class TelegramNotifier:
    def __init__(self):
        self.bot = Bot(Config.TELEGRAM_BOT_TOKEN)

    def format_signal(self, s):
        l = s['levels']

        msg = f'''
*{s['signal']} SIGNAL*

Pair: `{s['symbol']}`
Pattern: {s['pattern']}
Confidence: {s['confidence']} ({s['quality']}/5)

ENTRY & EXITS

Entry: `{l['entry']:.4f}`

Stop Loss: `{l['stop_loss']:.4f}`
Risk: {l['risk_pct']:.2f}%

Take Profits:
TP1: `{l['tp1']:.4f}` (1.5R)
TP2: `{l['tp2']:.4f}` (2.5R)
TP3: `{l['tp3']:.4f}` (4.0R)

CONDITIONS
Regime: {s['regime']['state']}
HTF Trend: {s['htf_trend']}
ATR Ratio: {s['regime']['atr_ratio']:.2f}

Time: {s['timestamp'].strftime('%H:%M:%S')}
'''
        return msg

    async def send_signal(self, signal):
        try:
            await self.bot.send_message(Config.TELEGRAM_CHAT_ID, self.format_signal(signal), parse_mode=ParseMode.MARKDOWN)
            print(f"Signal sent: {signal['symbol']}")
        except Exception as e:
            print(f"Send error: {e}")

async def main():
    print("CRYPTO SIGNAL BOT STARTED")
    print(f"Monitoring: {', '.join(Config.SYMBOLS)}")

    engine = CryptoEngine()
    notifier = TelegramNotifier()

    try:
        await notifier.bot.send_message(Config.TELEGRAM_CHAT_ID, "*Bot Online*\\nMonitoring signals...", parse_mode=ParseMode.MARKDOWN)
    except:
        print("Start message failed - check config")

    while True:
        try:
            print(f"Scanning at {datetime.now().strftime('%H:%M:%S')}...")

            for symbol in Config.SYMBOLS:
                signal = await engine.scan_symbol(symbol)
                if signal:
                    await notifier.send_signal(signal)
                    await asyncio.sleep(2)

            await asyncio.sleep(Config.SCAN_INTERVAL)
        except KeyboardInterrupt:
            print("Bot stopped")
            break
        except Exception as e:
            print(f"Error: {e}")
            await asyncio.sleep(60)

if __name__ == "__main__":
    asyncio.run(main())
