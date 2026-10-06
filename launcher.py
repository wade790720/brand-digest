"""打包成 .exe 的進入點。

兩種角色（同一支 exe）：
  - 前台模式（直接點 exe）：啟動本機伺服器 + 自動開瀏覽器。
  - 工作模式（前台當子行程呼叫）：exe --pipeline <網址...> → 跑抓取＋萃取。

用 PyInstaller onedir 打包，見 build_exe.py。
"""
import sys
import threading
import time
import webbrowser


def _worker():
    # 去掉 "--pipeline"，其餘參數原封不動交給 go.main（它用 argparse 解析）
    sys.argv = [sys.argv[0]] + sys.argv[2:]
    from go import main as go_main
    go_main()


def _server():
    import web
    # exe 旁邊也加進 PATH，方便放 ffmpeg.exe（雲端轉錄用不到，但保留彈性）
    import os
    os.environ["PATH"] = str(web.ROOT) + os.pathsep + os.environ.get("PATH", "")

    def open_browser():
        time.sleep(1.5)
        try:
            webbrowser.open(f"http://localhost:{web.PORT}")
        except Exception:
            pass
    threading.Thread(target=open_browser, daemon=True).start()
    web.serve()


if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()          # PyInstaller 多行程保險
    worker = len(sys.argv) > 1 and sys.argv[1] == "--pipeline"
    try:
        _worker() if worker else _server()
    except (SystemExit, KeyboardInterrupt):
        raise                                   # 正常結束與使用者按 Ctrl+C 不是錯誤
    except Exception as err:
        # 整個程式崩潰：留一份錯誤報告再結束，下次開啟可在「設定 → 錯誤回報」看到
        import traceback
        import reporting
        reporting.record("launcher", "crash", f"{type(err).__name__}: {err}", detail=traceback.format_exc(),
                         context={"mode": "pipeline" if worker else "server"})
        raise
