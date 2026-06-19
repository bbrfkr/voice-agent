"""音声 IO 非依存のオーケストレーション・コア。

Web フロント（server/）から使う再利用可能な部品をまとめる。
ここには PortAudio/ALSA/PulseAudio・マイク録音・音声再生を一切持ち込まない
（録音はブラウザの getUserMedia、再生はブラウザの AudioContext が担う）。
"""
