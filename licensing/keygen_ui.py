"""作者端轻量图形界面：CLI 的一层壳，不引入额外逻辑。

客户量小、人工收发机器码可接受，GUI 主要用来避免手滑填错天数或忘记录台账。
"""
from __future__ import annotations

import os
import tkinter as tk
from tkinter import ttk, messagebox, filedialog, scrolledtext

from licensing import keygen, rsa_sign, clock


class KeygenWindow:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("许可证签发器")
        root.geometry("760x720")
        self._build()

    def _build(self):
        pad = {"padx": 8, "pady": 4}
        top = ttk.Frame(self.root)
        top.pack(fill="x", **pad)

        ttk.Label(top, text="订单号").grid(row=0, column=0, sticky="w")
        self.order_var = tk.StringVar(value="A001")
        ttk.Entry(top, textvariable=self.order_var, width=20).grid(row=0, column=1, sticky="w")

        ttk.Label(top, text="类型").grid(row=0, column=2, sticky="w")
        self.type_var = tk.StringVar(value="限时")
        ttk.Combobox(top, textvariable=self.type_var, width=10, state="readonly",
                     values=["永久", "限时"]).grid(row=0, column=3, sticky="w")

        ttk.Label(top, text="时长").grid(row=1, column=0, sticky="w")
        self.duration_var = tk.StringVar(value="7")
        ttk.Entry(top, textvariable=self.duration_var, width=8).grid(row=1, column=1, sticky="w")
        self.unit_var = tk.StringVar(value="天")
        ttk.Combobox(top, textvariable=self.unit_var, width=6, state="readonly",
                     values=["天", "小时"]).grid(row=1, column=2, sticky="w")

        ttk.Label(self.root, text="激活请求（客户发来的机器码文本）").pack(anchor="w", **pad)
        self.request_text = scrolledtext.ScrolledText(self.root, height=6)
        self.request_text.pack(fill="both", **pad)

        buttons = ttk.Frame(self.root)
        buttons.pack(fill="x", **pad)
        ttk.Button(buttons, text="导入请求文件", command=self.load_request).pack(side="left", padx=4)
        ttk.Button(buttons, text="生成许可证", command=self.do_sign).pack(side="left", padx=4)
        ttk.Button(buttons, text="续期", command=self.do_renew).pack(side="left", padx=4)
        ttk.Button(buttons, text="生成撤销令", command=self.do_revoke).pack(side="left", padx=4)
        ttk.Button(buttons, text="验证退款回执", command=self.do_verify).pack(side="left", padx=4)

        ttk.Label(self.root, text="输出（可直接复制发给客户）").pack(anchor="w", **pad)
        self.output = scrolledtext.ScrolledText(self.root, height=10)
        self.output.pack(fill="both", expand=True, **pad)

        bottom = ttk.Frame(self.root)
        bottom.pack(fill="x", **pad)
        ttk.Button(bottom, text="复制输出", command=self.copy_output).pack(side="left", padx=4)
        ttk.Button(bottom, text="保存为 .lic", command=self.save_output).pack(side="left", padx=4)
        ttk.Button(bottom, text="刷新台账", command=self.show_ledger).pack(side="left", padx=4)

        self.status = tk.StringVar(value="就绪")
        ttk.Label(self.root, textvariable=self.status).pack(anchor="w", **pad)
        self.show_ledger()

    def _set_output(self, text: str):
        self.output.delete("1.0", "end")
        self.output.insert("1.0", text)

    def _duration(self) -> int:
        try:
            value = float(self.duration_var.get())
        except ValueError:
            raise ValueError("时长必须填数字")
        return int(value * (86400 if self.unit_var.get() == "天" else 3600))

    def load_request(self):
        path = filedialog.askopenfilename(title="选择激活请求",
                                          filetypes=[("文本", "*.txt"), ("全部", "*.*")])
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8-sig") as fh:
                text = fh.read().lstrip("\ufeff")
        except (OSError, UnicodeError) as exc:
            messagebox.showerror("导入失败", "文件读取失败（请用 UTF-8 文本）：%s" % exc)
            return
        self.request_text.delete("1.0", "end")
        self.request_text.insert("1.0", text)
        self.status.set("已导入请求：%s" % os.path.basename(path))

    def _request(self) -> str:
        text = self.request_text.get("1.0", "end").strip()
        if not text:
            raise ValueError("请先粘贴客户的激活请求")
        return text

    def do_sign(self):
        try:
            typ = "perpetual" if self.type_var.get() == "永久" else "timed"
            duration = 0 if typ == "perpetual" else self._duration()
            token = keygen.issue(self.order_var.get().strip(), self._request(),
                                 typ, duration)
            self._set_output(rsa_sign.split_lines(token))
            self.status.set("已签发 %s，台账已记录" % self.order_var.get().strip())
            self.show_ledger()
        except Exception as exc:
            messagebox.showerror("签发失败", str(exc))

    def do_renew(self):
        try:
            token = keygen.renew(self.order_var.get().strip(), self._duration())
            self._set_output(rsa_sign.split_lines(token))
            self.status.set("已续期，seq 递增")
            self.show_ledger()
        except Exception as exc:
            messagebox.showerror("续期失败", str(exc))

    def do_revoke(self):
        # keygen.make_revocation() 现在要求可信时间（与客户端首次激活对称），
        # NTP 不可达会抛 RuntimeError；GUI 无控制台，必须弹窗说明而不是静默。
        try:
            token, _ = keygen.make_revocation(
                self.order_var.get().strip(), "用户退款")
            self._set_output(rsa_sign.split_lines(token))
            self.status.set("撤销令已生成；发给客户导入后，请索取回执截图/视频")
            self.show_ledger()
        except Exception as exc:
            messagebox.showerror("撤销令生成失败", str(exc))

    def do_verify(self):
        dialog = tk.Toplevel(self.root)
        dialog.title("验证退款回执")
        dialog.geometry("420x180")
        ttk.Label(dialog, text="回执码").grid(row=0, column=0, padx=8, pady=8, sticky="w")
        code = tk.StringVar()
        ttk.Entry(dialog, textvariable=code, width=30).grid(row=0, column=1, padx=8, pady=8)
        ttk.Label(dialog, text="回执上的 Unix 秒").grid(row=1, column=0, padx=8, pady=8, sticky="w")
        at = tk.StringVar()
        ttk.Entry(dialog, textvariable=at, width=30).grid(row=1, column=1, padx=8, pady=8)
        result = tk.StringVar(value="")
        ttk.Label(dialog, textvariable=result, wraplength=380).grid(row=3, column=0, columnspan=2, padx=8)

        def run():
            try:
                ok, reason = keygen.verify_receipt(self.order_var.get().strip(),
                                                   code.get().strip(), int(at.get().strip()))
                result.set(("有效 - " if ok else "无效 - ") + reason)
            except Exception as exc:
                result.set("输入有误：%s" % exc)

        def close():
            try:
                dialog.grab_release()
            except tk.TclError:
                pass
            dialog.destroy()

        ttk.Button(dialog, text="核验", command=run).grid(row=2, column=0, columnspan=2, pady=10)
        ttk.Button(dialog, text="关闭", command=close).grid(row=4, column=0, columnspan=2, pady=6)
        dialog.protocol("WM_DELETE_WINDOW", close)
        dialog.transient(self.root)
        dialog.grab_set()
        self.root.wait_window(dialog)

    def copy_output(self):
        text = self.output.get("1.0", "end").strip()
        if text:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            self.status.set("已复制到剪贴板")

    def save_output(self):
        text = self.output.get("1.0", "end").strip()
        if not text:
            return
        path = filedialog.asksaveasfilename(defaultextension=".lic",
                                            initialfile="%s.lic" % self.order_var.get().strip(),
                                            filetypes=[("许可证", "*.lic")])
        if path:
            try:
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(text + "\n")
            except OSError as exc:
                messagebox.showerror("保存失败", str(exc))
                return
            self.status.set("已保存：%s" % path)

    def show_ledger(self):
        try:
            rows = keygen.read_ledger()
            lines = ["%s  %s  seq=%s  %s  %s" % (
                r.get("kind", ""), r.get("order", ""), r.get("seq", 0),
                r.get("type", ""), clock.format_utc(int(r.get("iat", 0))))
                for r in rows[-12:]]
            self.status.set("台账 %d 条（最近 12 条）： %s" % (len(rows), " | ".join(lines)))
        except Exception as exc:
            self.status.set("台账读取失败：%s" % exc)


def launch():
    root = tk.Tk()
    KeygenWindow(root)
    root.mainloop()
