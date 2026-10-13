# FPZ-31AT Bridge

[English](README.en.md)

**Design Pro以外のCAM・G-code送信ソフトから、MITS FPZ-31AT基板加工機を使うための変換ブリッジを開発しています。** 既存コントローラを利用し、標準的なG-code側とFPZ固有のUSB通信をつなぐことが目標です。

```text
OSS CAM → G-code → G-code sender → FPZ-31AT Bridge → FPZ USB controller
```

CAMによる加工経路の生成、送信ソフトによるジョブ管理、ブリッジによる機種固有命令への変換を分けます。現在はGRBL風のシリアル接続を入口にしており、Candleでオフライン検証しています。特定のCAMへの依存はありませんが、各CAMのG-code出力や他の送信ソフトとの互換性は今後の検証対象です。

2026年9月のリバースエンジニアリングで得た通信仕様と実装を、この開発の土台として収録しています。

**研究段階です。Candleから実機を加工できる完成済みドライバではありません。** USB送信成功、合成応答、シミュレーション上のIdleを、実機の移動完了や停止確認として扱いません。

## 確認できたこと

| 対象 | 得られた結果 | 根拠・限界 |
| --- | --- | --- |
| USB | `140c:0001`、interrupt IN `0x81`、bulk OUT `0x02`、各64 byte | 保存済みdescriptorとドライバ静的解析。2026-10-13にもsysfsで構成を確認 |
| XY | `M dx,dy\r` / `MA x,y\r`、符号付き整数count | 静的解析。相対移動のGUI生成を合成デバイスで照合 |
| 単位 | 調査時のGUIでは1 mm → 157 count、選択scaleは0.00635 | 特定の設定でのソフトウェア挙動。実機の移動量は未測定 |
| 速度 | GUIの2.5 / 5.5 / 7.9 mm/sec → `S 2` / `S 5` / `S 7` | 合成デバイスでの送信を確認。実速度・安全範囲は未測定 |
| Z | Downは`P1`→`P2`、Upは`P0`というプリセット選択 | 任意のG-code Z座標やXYZ同時補間への変換は未確立 |
| 応答 | `RP`、位置payload、XON/XOFF、アプリ側エラー分類 | 静的に解析。実機から有効な位置応答は取得できていない |
| Candle | GRBL風PTYブリッジ、シミュレーション、XY計画器 | オフライン69テスト。過去のCandle 1.1.8 GUI検証で8行のジョブ完了 |
| 停止・完了 | 未確立 | `C`、`F`、ESCを即時停止や機械的完了の証拠にしない |

## まずオフラインで動かす

Linux/POSIXとPython 3.11以上を使用します。

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[test]'
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m fpz_candle --mode simulation
```

インストール後は`.venv/bin/fpz-31at-bridge --mode simulation`でも起動できます。既存コードのPythonモジュール名`fpz_candle`と`fpz-candle`コマンドは互換性のため保持しています。

標準出力の最初の行に出る`/dev/pts/N`をCandleのPortへ入力します（baud 115200）。実機は不要です。シミュレーションの座標・状態・完了通知は合成値です。

引数なしの`python -m fpz_candle`は`unavailable`モードです。USBを開かず、座標を出さず、移動を拒否します。CLIに実機モードはありません。詳細は[ブリッジの仕様](docs/bridge.md)を参照してください。

## 資料

- [解析手法と根拠](docs/methodology.md)：対象バイナリ、アドレス、合成応答の扱い
- [USBトランスポート](docs/usb-transport.md)：専用ドライバ、endpoint対応、EXE内蔵実装
- [コマンド・応答仕様](docs/protocol.md)：確認済み形式、変換、Z・停止の未解決点
- [検証結果](docs/validation.md)：静的解析、合成GUI、テスト、実機観測を区別
- [実機検証の前提](docs/hardware-validation.md)：次回の限定した観測計画
- [貢献方法](CONTRIBUTING.md)：根拠を添えた変更と再現手順
- [公開範囲とライセンス](docs/publication.md)

`fpz_candle/`には純粋なencoder/decoder、XY計画器、明示的に許可しない限り開かないUSB transport、PTY runtimeを収録しています。実機codecの統合は未完了です。`evidence/`には公開用に整理した小さな記録だけを収録しています。

## 今後の課題

1. 前提を確認した機体で、1回の`RP`と直後の連続受信を観測する。
2. countと実測座標、原点、軸方向、速度の関係を確立する。
3. 完了応答と物理停止を確認し、異常時に安全に止める方法を確立する。
4. 上記を満たしてからCandleの実機codecを統合する。Z、主軸、工具交換は別途検証する。

純正ソフトウェア、ドライバ、機体固有設定、ライセンス情報は同梱していません。本プロジェクトはMITSによる公式製品ではありません。

## ライセンス

独自のコード・文書は[MIT License](LICENSE)。プロトコルの相互運用と調査結果の再利用を目的としています。外部ソフトウェアやメーカー文書の権利を付与するものではありません。
