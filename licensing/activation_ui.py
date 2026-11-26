"""客户端激活窗口：展示机器码、导入许可证、导入撤销令并显示退款回执。"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk, messagebox, filedialog, scrolledtext

from licensing import license_core, fingerprint, rsa_sign


def _copy(widget: tk.Misc, text: str) -> None:
    widget.clipboard_clear()
    widget.clipboard_append(text)


def _import_revocation(token: str, status_set, show_receipt) -> None:
    """导入撤销令的核心逻辑，供 Tk 回调调用。

    状态写入可能因磁盘失败抛 OSError、可信时间可能抛异常；GUI 没有控制台，
    必须在此收敛并弹窗，否则异常会从 Tk 回调逸出、打印 traceback 且客户
    看不到任何提示（与作者端 keygen_ui.do_revoke 口径一致）。
    """
    try:
        ok, receipt, why = license_core.apply_revocation(token)
        if ok:
            status_set("本产品已停用，请保留回执截图/视频")
            show_receipt(receipt)
        else:
            status_set("撤销失败：%s" % why)
    except Exception as exc:
        messagebox.showerror("撤销令导入失败", str(exc))


def show_activation_dialog(parent: tk.Misc | None, reason: str | None = None) -> bool:
    """模态窗口；返回是否在本次会话中成功激活。"""
    own_root = False
    if parent is None:
        parent = tk.Tk()
        parent.withdraw()
        own_root = True

    dialog = tk.Toplevel(parent)
    dialog.title("产品激活")
    dialog.geometry("680x680")
    # 不能 transient 到主窗口：launcher 启动时会 root.withdraw() 隐藏主窗口，
    # Windows 上 Toplevel 依附隐藏的 transient master 会一并变成 withdrawn，
    # 导致激活窗口永远不显示、程序卡在 wait_window（双击 exe 像没反应）。
    # 激活对话框本就是独立模态窗口，grab_set + lift 已足够保证它floating在最前。
    result = {"ok": False}

    pad = {"padx": 10, "pady": 5}
    if reason:
        ttk.Label(dialog, text="当前状态：%s" % reason, foreground="#A32D2D").pack(anchor="w", **pad)

    summary = license_core.license_summary()
    if summary:
        ttk.Label(dialog, text="已记录授权：%s（seq=%s）%s" % (
            summary["order"], summary["max_seq"],
            " [已作废]" if summary["revoked"] else "")).pack(anchor="w", **pad)

    machine = fingerprint.machine_code()
    request_text = license_core.build_request()

    # 激活请求里已含机器码（mid 字段），签发器只吃请求；机器码仅留作对账核对，
    # 不再单独提供复制按钮，避免客户误以为要发两段内容。
    ttk.Label(dialog, text="机器码（供核对，无需发送）",
              font=("TkDefaultFont", 10, "bold")).pack(anchor="w", **pad)
    row = ttk.Frame(dialog)
    row.pack(fill="x", **pad)
    ttk.Label(row, text=machine, font=("Consolas", 14)).pack(side="left")

    ttk.Label(dialog, text="激活请求（复制这一段，或导出文件发给作者）").pack(anchor="w", **pad)
    req_box = scrolledtext.ScrolledText(dialog, height=5)
    req_box.pack(fill="both", **pad)
    req_box.insert("1.0", rsa_sign.split_lines(request_text))
    req_box.configure(state="disabled")

    req_buttons = ttk.Frame(dialog)
    req_buttons.pack(fill="x", **pad)
    ttk.Button(req_buttons, text="复制激活请求",
               command=lambda: _copy(dialog, request_text)).pack(side="left", padx=4)

    def export_request():
        path = filedialog.asksaveasfilename(defaultextension=".txt",
                                            initialfile="activation_request.txt",
                                            filetypes=[("文本", "*.txt")])
        if path:
            try:
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(request_text + "\n")
            except OSError as exc:
                messagebox.showerror("导出失败", str(exc))
                return
            messagebox.showinfo("已导出", "请把文件发给作者：%s" % path)

    ttk.Button(req_buttons, text="导出请求文件", command=export_request).pack(side="left", padx=4)

    ttk.Label(dialog, text="许可证 / 撤销令（粘贴或导入文件）").pack(anchor="w", **pad)
    token_box = scrolledtext.ScrolledText(dialog, height=7)
    token_box.pack(fill="both", **pad)

    def import_file():
        path = filedialog.askopenfilename(filetypes=[("许可证", "*.lic;*.txt"), ("全部", "*.*")])
        if not path:
            return
        # 客户用记事本另存可能带 UTF-8 BOM 或 GBK，给明确提示而不是让 Tk 回调抛异常
        try:
            with open(path, "r", encoding="utf-8-sig") as fh:
                text = fh.read().lstrip("\ufeff")
        except (OSError, UnicodeError) as exc:
            messagebox.showerror("导入失败", "文件读取失败（请用 UTF-8 文本）：%s" % exc)
            return
        token_box.delete("1.0", "end")
        token_box.insert("1.0", text)

    token_buttons = ttk.Frame(dialog)
    token_buttons.pack(fill="x", **pad)
    ttk.Button(token_buttons, text="导入 .lic 文件", command=import_file).pack(side="left", padx=4)

    status = tk.StringVar(value="")
    ttk.Label(dialog, textvariable=status, wraplength=640).pack(anchor="w", **pad)

    def show_receipt(receipt: dict):
        win = tk.Toplevel(dialog)
        win.title("撤销回执 - 请截图或录屏发给作者办理退款")
        win.geometry("520x320")
        lines = [
            "订单号：%s" % receipt["order"],
            "机器码：%s" % receipt["machine_code"],
            "停用时间：%s" % receipt["revoked_at_utc"],
            "Unix 秒：%d" % receipt["revoked_at"],
            "回执码：%s" % receipt["code"],
            "原因：%s" % receipt["reason"],
            "",
            "请把以上信息截图或录屏发给作者；作者会用签发工具复算回执码核验。",
        ]
        text = scrolledtext.ScrolledText(win)
        text.pack(fill="both", expand=True, padx=10, pady=10)
        text.insert("1.0", "\n".join(lines))
        text.configure(state="disabled")
        buttons = ttk.Frame(win)
        buttons.pack(pady=6)
        ttk.Button(buttons, text="复制回执",
                   command=lambda: _copy(win, "\n".join(lines))).pack(side="left", padx=4)

        def close_receipt():
            try:
                win.grab_release()
            except tk.TclError:
                pass
            win.destroy()
            try:
                dialog.grab_set()  # 把模态焦点还给激活窗口
            except tk.TclError:
                pass

        ttk.Button(buttons, text="关闭", command=close_receipt).pack(side="left", padx=4)
        win.protocol("WM_DELETE_WINDOW", close_receipt)
        win.transient(dialog)
        # 模态等待：回执窗口关闭前不回到激活对话框，避免客户没截图就关掉。
        # 先释放 dialog 的 grab 再设 win 的 grab，保证任意时刻只有一层；
        # 关闭时显式释放 win 并把唯一一层 grab 还给 dialog。
        try:
            dialog.grab_release()
        except tk.TclError:
            pass
        win.grab_set()
        dialog.wait_window(win)

    def close_dialog():
        try:
            dialog.grab_release()
        except tk.TclError:
            pass
        dialog.destroy()

    def do_activate():
        token = token_box.get("1.0", "end").strip().lstrip("\ufeff")
        if not token:
            status.set("请先粘贴或导入许可证")
            return
        # 首次激活会强制刷新 NTP 共识（最多约 2.5 秒），首次采集机器指纹
        # 还可能有 PowerShell 超时。先刷新 UI 给出明确进度，避免被当成死机。
        status.set("正在校准网络时间并校验许可证，请稍候…")
        try:
            activate_btn.configure(state="disabled")
            dialog.update_idletasks()
            ok, payload, why = license_core.activate(token)
        except Exception as exc:
            status.set("激活失败：%r" % (exc,))
            ok = False
        finally:
            try:
                activate_btn.configure(state="normal")
            except tk.TclError:
                pass
        if ok:
            status.set("激活成功：%s（%s）" % (payload["order"], payload["type"]))
            result["ok"] = True
            try:
                dialog.grab_release()
                dialog.destroy()
            except tk.TclError:
                pass
        else:
            try:
                status.set("激活失败：%s" % why)
            except (UnboundLocalError, tk.TclError):
                pass

    def do_revoke():
        token = token_box.get("1.0", "end").strip().lstrip("\ufeff")
        if not token:
            status.set("请先粘贴或导入撤销令")
            return
        _import_revocation(token, status.set, show_receipt)

    actions = ttk.Frame(dialog)
    actions.pack(fill="x", **pad)
    activate_btn = ttk.Button(actions, text="激活", command=do_activate)
    activate_btn.pack(side="left", padx=4)
    ttk.Button(actions, text="导入撤销令（退款）", command=do_revoke).pack(side="left", padx=4)
    ttk.Button(actions, text="退出", command=close_dialog).pack(side="right", padx=4)

    dialog.protocol("WM_DELETE_WINDOW", close_dialog)
    dialog.grab_set()
    dialog.lift()
    parent.wait_window(dialog)
    if own_root:
        parent.destroy()
    return result["ok"]
