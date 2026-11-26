import win32api
import win32con
import win32ui
import win32process
import ctypes
import pyautogui
import cv2
import numpy as np
from win32 import win32gui
import time
import random
import os
import sys
import argparse
import atexit
import json
import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext
import threading

SKILL_LIST = [
    {"name": "枪械", "template": ["skill.png", "skill-1.png"]},
    {"name": "温压弹", "template": ["skill-wyd.png", "skill-wyd-1.png"]},
    {"name": "干冰弹", "template": ["skill-gbd.png", "skill-gbd-1.png"]},
    {"name": "冰雹发生器", "template": ["skill-bb.png", "skill-bb-1.png"]},
    {"name": "装甲车", "template": ["skill-c.png", "skill-c-1.png"]},
    {"name": "电磁穿刺", "template": ["skill-d.png", "skill-d-1.png"]},
    {"name": "压缩气刃", "template": ["skill-fr.png", "skill-fr-1.png"]},
    {"name": "制导激光", "template": ["skill-jg.png", "skill-jg-1.png"]},
    {"name": "旋风加农", "template": ["skill-ljf.png", "skill-ljf-1.png"]},
    {"name": "燃油弹", "template": ["skill-ry.png", "skill-ry-1.png"]},
    {"name": "高能射线", "template": ["skill-sx.png", "skill-sx-1.png"]},
    {"name": "无人机冲击", "template": ["skill-wrj.png", "skill-wrj-1.png"]},
    {"name": "跃迁电子", "template": ["skill-yq.png", "skill-yq-1.png"]},
    {"name": "空投轰炸", "template": ["skill-kt.png", "skill-kt-1.png"]},
    {"name": "空间裂隙", "template": ["skill-kjlx.png", "skill-kjlx-1.png"]},
]


class GameBot:
    def __init__(
        self,
        game_title="游戏窗口标题",
        battle_time=0,
        max_battle_count=0,
        mode=0,
        priority_skills=None,
        rich_mode=0,
        wait_time=60,
        quick_exit=False,
        background_mode=True,
        match_log_enabled=False,
        debug_mode=False,
        recruit_click_interval=0.05,
    ):
        self.running = True
        # 单步调试模式：主循环每轮开头阻塞在 step_event，由 GUI 点"下一步"放行一轮。
        # 用 threading.Event 做线程间同步，避免自动连跑刷屏，便于逐步观察决策。
        self.debug_mode = debug_mode
        self.step_event = threading.Event()
        self.step_count = 0
        # 热键用 GetAsyncKeyState 轮询，不用 pynput 全局钩子（会打断微信输入法组字）
        self._hotkey_thread = None
        self._hotkey_esc_down = False
        self._hotkey_f8_down = False
        self._mouse_left_down = False
        # 记录用户最近一次点击的位置和时间（用于判断是否在主动操作游戏）
        self._last_click_pos = None
        self._last_click_time = 0
        # 临时查看窗口的截止时间（F8触发后几秒内不隐藏）
        self._peek_until = 0
        """初始化游戏机器人"""
        self.game_title = game_title
        # 后台模式：截图用PrintWindow、点击用PostMessage，不占用真实鼠标键盘
        self.background_mode = background_mode
        self.hwnd = None
        self.battle_time = battle_time
        self.current_battle_time = 0
        self.max_battle_count = max_battle_count
        self.game_window = None
        self.screenshot_dir = "screenshots"
        self.priority_skills = priority_skills if priority_skills else []
        self.rich_mode = rich_mode
        self.quick_exit = quick_exit
        self.on_battle_count_changed = None
        self.expedition_in_team_max_time = time.time() + wait_time
        self.wait_time = wait_time
        self.battle_count = 0
        self.last_battle_count_time = 0
        # 深渊挑战：本次运行已发起的挑战次数（点一次"挑战"算一次）
        self.abyss_count = 0
        self.abyss_no_chance_logged = False
        # 深渊"待战斗"状态：点挑战后进入，倒计时/强化弹窗/选技能/战斗期间一律不导航。
        # 用状态而非死等时间（倒计时约10秒，死等8秒会在弹窗出现前误判为不在深渊页）。
        self.abyss_pending_battle = False
        self.abyss_pending_since = 0

        # 元素试炼：本次运行已完成轮数、次数用完日志去重
        self.element_count = 0
        self.element_no_chance_logged = False
        # 次数耗尽需连续N次确认"在详情页但挑战按钮点不动"，防单次误判提前退出
        self.element_no_chance_streak = 0
        # 元素试炼"待战斗"状态：点"挑战"进入，选技能/战斗/结算期间一律不导航。
        # 结算页只有约10秒窗口，靠状态+每轮高频检测"下一关"按钮接手，不用死等。
        self.element_pending_battle = False
        self.element_pending_since = 0
        # 结算页点"下一关"后的过渡标记：游戏会直接进入下一局(可能跳过详情页和
        # 开始游戏弹窗)。此期间严禁导航，否则会被误判成"不在任何页面"而跑回大厅。
        self.element_after_next = False
        # 历练大厅下滑计数：元素试炼在最底部，需连续下滑多次才露出，
        # 期间不依赖易滚出屏幕的深渊标题判断，避免中途被误判回首页反复重导。
        self.element_scroll_count = 0

        # 前台压制：记录用户最近使用的前台窗口，游戏抢前台时立即压回
        self._last_foreground = None
        self._suppress_thread = None
        self._noactivate_hwnd = None
        self._orig_exstyle = None
        # 窗口隐藏：把游戏窗口移到屏幕外，被抢前台也完全不可见
        self._hidden_hwnd = None
        self._hidden_orig_pos = None
        # 进程退出时兜底恢复游戏窗口（防止异常退出后窗口留在屏幕外）
        atexit.register(self._restore_window_state)

        if getattr(sys, "frozen", False):
            self.template_dir = os.path.join(sys._MEIPASS, "templates")
        else:
            self.template_dir = "templates"

        self.mode = mode

        self.template_cache = {}
        # 匹配诊断日志：记录模板匹配分数，便于排查"UI改版导致模板失配"类问题。
        # 由 GUI 复选框控制，默认关闭，避免日常运行时产生大量日志。
        self.match_log_enabled = match_log_enabled
        self._match_log_state = {}      # {模板名: (状态桶, 上次打印时间)}
        self.MATCH_LOG_INTERVAL = 20    # 同一模板状态不变时的最短重复间隔(秒)
        self.MATCH_LOG_NEAR = 0.28      # 低于阈值但在该范围内记为"接近阈值"(重点诊断信号)
        # 抢队：点间隔 + 按「新队伍出现」统计成功率（旧队空点不计入）
        self.recruit_click_interval = max(0.01, float(recruit_click_interval))
        self._recruit_prev_ys = set()
        self._recruit_pending_since = None
        self._recruit_new = 0
        self._recruit_ok = 0
        self._recruit_fail = 0
        self.RECRUIT_RACE_SEC = 2.0
        self.RECRUIT_Y_BUCKET = 20
        self._preload_templates()

        self._screenshot_bgr = None
        self._screenshot_time = 0
        self._screenshot_ttl = 0.1

        os.makedirs(self.screenshot_dir, exist_ok=True)

    def _preload_templates(self):
        """预加载所有模板图像到内存缓存"""
        if not os.path.exists(self.template_dir):
            print(f"模板目录不存在: {self.template_dir}")
            return

        loaded_count = 0
        skipped_count = 0
        for filename in os.listdir(self.template_dir):
            if filename.lower().endswith((".png", ".jpg", ".jpeg")):
                template_path = os.path.join(self.template_dir, filename)
                template = cv2.imread(template_path)
                if template is None:
                    continue
                # 跳过超大模板（如battling-stop.png是2160x2160全屏大图）：
                # 它们比任何游戏窗口截图都大，matchTemplate永远用不上，还浪费内存和加载时间
                h, w = template.shape[:2]
                if h > 2000 or w > 2000:
                    skipped_count += 1
                    continue
                self.template_cache[filename] = template
                loaded_count += 1

        print(f"模板预加载完成: {loaded_count} 个模板已加载到内存" + (f"，跳过 {skipped_count} 个超大模板" if skipped_count else ""))

    # ==================== 后台模式基础设施 ====================

    def _get_hwnd(self):
        """获取并缓存游戏窗口句柄"""
        if self.hwnd and win32gui.IsWindow(self.hwnd):
            return self.hwnd
        self.hwnd = win32gui.FindWindow(None, self.game_title)
        return self.hwnd

    def _get_click_hwnd(self):
        """点击目标窗口：优先 Chrome 渲染子窗口（微信小程序实际收鼠标消息的层）。"""
        hwnd = self._get_hwnd()
        if not hwnd:
            return None
        chrome = None

        def _enum(h, _):
            nonlocal chrome
            try:
                if win32gui.GetClassName(h) == "Chrome_RenderWidgetHostHWND":
                    chrome = h
            except Exception:
                pass

        try:
            win32gui.EnumChildWindows(hwnd, _enum, None)
        except Exception:
            pass
        return chrome or hwnd

    def _post_mouse(self, x, y, msg, wparam):
        """向游戏窗口客户区发送鼠标消息（屏幕坐标 -> 客户区坐标，不移动真实鼠标）"""
        hwnd = self._get_click_hwnd()
        if not hwnd:
            return
        client_x, client_y = win32gui.ScreenToClient(hwnd, (int(x), int(y)))
        # 手动打包lparam（部分pywin32版本没有MAKELPARAM）
        lparam = (client_y & 0xFFFF) << 16 | (client_x & 0xFFFF)
        win32gui.PostMessage(hwnd, msg, wparam, lparam)

    def _post_click(self, x, y, press_delay=0.05):
        """后台点击：发送鼠标按下/抬起消息，不移动真实鼠标"""
        self._post_mouse(x, y, win32con.WM_MOUSEMOVE, 0)
        time.sleep(0.02)
        self._post_mouse(x, y, win32con.WM_LBUTTONDOWN, win32con.MK_LBUTTON)
        time.sleep(press_delay)
        self._post_mouse(x, y, win32con.WM_LBUTTONUP, 0)

    def _post_drag(self, x1, y1, x2, y2, duration=0.3):
        """后台拖动：分步发送鼠标消息模拟滑动（减速收尾，松开后不触发惯性滚动）"""
        steps = 12
        delay = max(0.01, duration / steps)
        self._post_mouse(x1, y1, win32con.WM_MOUSEMOVE, 0)
        time.sleep(0.05)
        self._post_mouse(x1, y1, win32con.WM_LBUTTONDOWN, win32con.MK_LBUTTON)
        time.sleep(0.05)
        # ease-out：先快后慢，接近终点时速度趋近0
        for i in range(1, steps + 1):
            t = 1 - (1 - i / steps) ** 2
            cur_x = x1 + (x2 - x1) * t
            cur_y = y1 + (y2 - y1) * t
            self._post_mouse(cur_x, cur_y, win32con.WM_MOUSEMOVE, win32con.MK_LBUTTON)
            time.sleep(delay)
        # 在终点按住不动片刻，让速度彻底归零后再松开，避免页面继续滑动
        for _ in range(3):
            self._post_mouse(x2, y2, win32con.WM_MOUSEMOVE, win32con.MK_LBUTTON)
            time.sleep(0.05)
        self._post_mouse(x2, y2, win32con.WM_LBUTTONUP, 0)

    def _post_key(self, key, presses=1, interval=0.1):
        """后台按键：向窗口发送键盘消息"""
        hwnd = self._get_hwnd()
        if not hwnd:
            return

        # 键名 -> 虚拟键码映射
        key_map = {
            "esc": win32con.VK_ESCAPE,
            "enter": win32con.VK_RETURN,
            "return": win32con.VK_RETURN,
            "space": win32con.VK_SPACE,
            "backspace": win32con.VK_BACK,
            "delete": win32con.VK_DELETE,
            "del": win32con.VK_DELETE,
            "tab": win32con.VK_TAB,
            "up": win32con.VK_UP,
            "down": win32con.VK_DOWN,
            "left": win32con.VK_LEFT,
            "right": win32con.VK_RIGHT,
        }
        vk = key_map.get(key.lower())
        if vk is None and len(key) == 1:
            vk = ord(key.upper())

        if vk is None:
            # 无法映射的键回退前台模式
            print(f"按键 {key} 无法后台发送，使用前台模式")
            pyautogui.press(key, presses=presses, interval=interval)
            return

        lparam_down = 1 | (win32api.MapVirtualKey(vk, 0) << 16)
        lparam_up = lparam_down | (1 << 30) | (1 << 31)
        for _ in range(presses):
            win32gui.PostMessage(hwnd, win32con.WM_KEYDOWN, vk, lparam_down)
            win32gui.PostMessage(hwnd, win32con.WM_CHAR, vk, lparam_down)
            time.sleep(max(0.05, interval))
            win32gui.PostMessage(hwnd, win32con.WM_KEYUP, vk, lparam_up)
            print(f"后台按下按键: {key}")

    def _restore_foreground(self, target_hwnd):
        """把前台还给指定窗口（AttachThreadInput绕过前台锁定限制）
        注意：pywin32 312 的 win32api 没有 AttachThreadInput，必须走 ctypes.user32"""
        if not target_hwnd or not win32gui.IsWindow(target_hwnd) or win32gui.IsIconic(target_hwnd):
            return
        try:
            cur_thread = win32api.GetCurrentThreadId()
            fg = win32gui.GetForegroundWindow()
            threads = set()
            for hwnd in (fg, target_hwnd):
                if hwnd:
                    tid, _ = win32process.GetWindowThreadProcessId(hwnd)
                    if tid and tid != cur_thread:
                        threads.add(tid)
            # 用ctypes调用user32.AttachThreadInput（win32api没有这个函数）
            for tid in threads:
                ctypes.windll.user32.AttachThreadInput(cur_thread, tid, True)
            try:
                win32gui.SetForegroundWindow(target_hwnd)
                win32gui.BringWindowToTop(target_hwnd)
            finally:
                for tid in threads:
                    ctypes.windll.user32.AttachThreadInput(cur_thread, tid, False)
        except Exception as e:
            # 不再静默吞掉：至少打一次日志便于排查
            now = time.time()
            if now - getattr(self, "_last_fg_log", 0) >= 5:
                self._last_fg_log = now
                print(f"归还焦点失败: {e}")

    def _ensure_noactivate(self):
        """给游戏窗口加WS_EX_NOACTIVATE样式，从根源上禁止它激活自己置顶
        （微信调用SetForegroundWindow会直接失败，不会闪现窗口；游戏窗口关闭重开后自动重新应用）"""
        hwnd = self._get_hwnd()
        if not hwnd or hwnd == self._noactivate_hwnd:
            return
        try:
            exstyle = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
            # WS_EX_APPWINDOW保证加了防激活样式后任务栏图标仍然显示
            win32gui.SetWindowLong(
                hwnd,
                win32con.GWL_EXSTYLE,
                exstyle | win32con.WS_EX_NOACTIVATE | win32con.WS_EX_APPWINDOW,
            )
            self._noactivate_hwnd = hwnd
            self._orig_exstyle = exstyle
            print("已为游戏窗口启用防激活(WS_EX_NOACTIVATE)，它将无法再抢前台")
        except Exception as e:
            print(f"设置防激活样式失败: {e}")

    def _remove_noactivate(self):
        """脚本停止时恢复游戏窗口原始样式，方便用户手动操作游戏"""
        hwnd = self._noactivate_hwnd
        if not hwnd:
            return
        try:
            if win32gui.IsWindow(hwnd):
                win32gui.SetWindowLong(hwnd, win32con.GWL_EXSTYLE, self._orig_exstyle)
            print("已恢复游戏窗口原始样式")
        except Exception:
            pass
        finally:
            self._noactivate_hwnd = None
            self._orig_exstyle = None

    def _is_above_foreground(self, hwnd, fg):
        """判断游戏窗口是否叠在当前前台窗口之上（未激活但被单独置顶的情况）"""
        if not fg or fg == hwnd:
            return False
        h = win32gui.GetTopWindow(None)
        while h:
            if h == hwnd:
                return True
            if h == fg:
                return False
            h = win32gui.GetWindow(h, win32con.GW_HWNDNEXT)
        return False

    def _push_game_down(self, hwnd, restore_focus=False):
        """把游戏窗口压回窗口堆叠底部。

        默认不再 SetForegroundWindow 抢回微信等窗口：找队伍时游戏频繁闪前台，
        若强行把焦点塞回聊天框，会打断输入法/清空正在打的字。
        只把游戏沉底即可，焦点留给系统自然处理。
        """
        # 只在用户最近3秒内真实点击过游戏窗口时才不压制（用户在主动操作游戏）。
        # 鼠标只是悬停不算——游戏窗口占屏幕很大，悬停很常见，不能因此跳过压制。
        try:
            if self._last_click_pos and time.time() - self._last_click_time <= 3:
                l, t, r, b = win32gui.GetWindowRect(hwnd)
                cx, cy = self._last_click_pos
                if l <= cx <= r and t <= cy <= b:
                    return
        except Exception:
            pass
        flags = win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_NOACTIVATE
        try:
            win32gui.SetWindowPos(hwnd, win32con.HWND_NOTOPMOST, 0, 0, 0, 0, flags)
            win32gui.SetWindowPos(hwnd, win32con.HWND_BOTTOM, 0, 0, 0, 0, flags)
            if restore_focus and self._last_foreground:
                self._restore_foreground(self._last_foreground)
            # 日志节流：同一消息最多2秒打一次，避免刷屏
            now = time.time()
            if now - getattr(self, "_last_push_log", 0) >= 2:
                self._last_push_log = now
                print("检测到游戏窗口抢前台，已压回后台")
        except Exception:
            pass

    def _keep_game_background(self):
        """兜底监测：万一游戏窗口仍抢到前台或被单独置顶，立刻压回（不抢焦点）"""
        hwnd = self._get_hwnd()
        if not hwnd:
            return
        fg = win32gui.GetForegroundWindow()
        if fg == hwnd:
            self._push_game_down(hwnd, restore_focus=False)
        else:
            if fg:
                self._last_foreground = fg
            if self._is_above_foreground(hwnd, fg):
                self._push_game_down(hwnd, restore_focus=False)

    def _ensure_hidden(self):
        """把游戏窗口移到屏幕外隐藏：彻底不打扰用户、不闪烁，
        而PrintWindow截图和模板识别不受窗口位置影响（已验证屏幕外识别正常）。
        微信偶尔会恢复/缩放窗口，这里持续强制移回屏幕外并保持正确尺寸。"""
        hwnd = self._get_hwnd()
        if not hwnd:
            return
        try:
            # 用户请求临时查看窗口（按快捷键后几秒内不隐藏）
            if getattr(self, "_peek_until", 0) and time.time() < self._peek_until:
                if win32gui.IsIconic(hwnd):
                    win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                return

            # 最小化窗口无法截图，先恢复（恢复后可能回屏幕内，下一轮再藏）
            if win32gui.IsIconic(hwnd):
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                time.sleep(0.3)
                return

            left, top, right, bottom = win32gui.GetWindowRect(hwnd)
            win_w, win_h = right - left, bottom - top

            # 目标：放在虚拟屏幕左边界之外（屏幕外左侧），保持542x1010客户区尺寸
            vx = win32api.GetSystemMetrics(76)  # SM_XVIRTUALSCREEN
            target_x = vx - 3000

            # 已在屏幕外且尺寸正确：无需操作
            if right <= vx and left >= target_x - 100:
                return

            # 窗口被微信恢复/缩放/移到别处：重新藏起来
            # 记录原始位置（仅第一次，供停止脚本时恢复）
            if not self._hidden_orig_pos:
                # 若窗口此刻是最小化(-32000)或屏幕外，先恢复/记录一个合理位置，
                # 避免停止脚本后窗口回不到屏幕内
                if left <= -30000 or top <= -30000:
                    self._hidden_orig_pos = (100, 100)
                else:
                    self._hidden_orig_pos = (left, top)

            # 强制正确尺寸（542x1010客户区 = 约558x1048窗口含边框）
            client_rect = win32gui.GetClientRect(hwnd)
            border_w = win_w - client_rect[2]
            border_h = win_h - client_rect[3]
            target_w = 542 + max(0, border_w)
            target_h = 1010 + max(0, border_h)

            win32gui.MoveWindow(hwnd, target_x, vy if (vy := win32api.GetSystemMetrics(77)) else 0, target_w, target_h, True)
            self._hidden_hwnd = hwnd
            # 日志节流：避免每次检测都打印
            now = time.time()
            if now - getattr(self, "_last_hide_log", 0) >= 5:
                self._last_hide_log = now
                print("已将游戏窗口移到屏幕外隐藏（停止脚本后自动移回）")
        except Exception as e:
            print(f"隐藏游戏窗口失败: {e}")

    def _restore_window_state(self):
        """脚本停止/进程退出：把游戏窗口移回原位置并恢复原始样式"""
        hwnd = self._hidden_hwnd
        if hwnd and self._hidden_orig_pos and win32gui.IsWindow(hwnd):
            try:
                # 若窗口当前被移出屏幕，先恢复到屏幕内原位置，并恢复542x1010推荐尺寸
                cur = win32gui.GetWindowRect(hwnd)
                client_rect = win32gui.GetClientRect(hwnd)
                border_w = (cur[2]-cur[0]) - client_rect[2]
                border_h = (cur[3]-cur[1]) - client_rect[3]
                target_w = 542 + max(0, border_w)
                target_h = 1010 + max(0, border_h)
                win32gui.MoveWindow(
                    hwnd,
                    self._hidden_orig_pos[0],
                    self._hidden_orig_pos[1],
                    target_w,
                    target_h,
                    True,
                )
                print("游戏窗口已移回屏幕")
            except Exception:
                pass
        self._hidden_hwnd = None
        self._hidden_orig_pos = None
        self._remove_noactivate()

    def _suppress_loop(self):
        """压制线程：隐藏窗口+防激活+焦点监测，游戏窗口抢前台时立即压制"""
        tick = 0
        while self.running:
            try:
                self._ensure_hidden()
                if tick % 10 == 0:
                    # 慢速任务(约0.5秒一次)：防激活样式 + Z序兜底检查
                    self._ensure_noactivate()
                    self._keep_game_background()
                else:
                    # 快速焦点监测(0.05秒一次)：游戏抢到前台只沉底，不 SetForeground 抢回微信
                    # （抢回聊天焦点会清空/打断正在输入的文字，找队伍时尤其频繁）
                    hwnd = self._get_hwnd()
                    if hwnd:
                        fg = win32gui.GetForegroundWindow()
                        if fg == hwnd:
                            self._push_game_down(hwnd, restore_focus=False)
                        elif fg:
                            self._last_foreground = fg
            except Exception:
                pass
            tick += 1
            time.sleep(0.05)
        # 脚本停止：恢复游戏窗口位置和样式
        self._restore_window_state()

    def _capture_window(self):
        """后台截图：PrintWindow抓取窗口内容（窗口可被遮挡，不能最小化）"""
        hwnd = self._get_hwnd()
        if not hwnd:
            return None

        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        win_w, win_h = right - left, bottom - top
        if win_w <= 0 or win_h <= 0:
            return None

        hwnd_dc = win32gui.GetWindowDC(hwnd)
        mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
        save_dc = mfc_dc.CreateCompatibleDC()
        bitmap = win32ui.CreateBitmap()
        bitmap.CreateCompatibleBitmap(mfc_dc, win_w, win_h)
        save_dc.SelectObject(bitmap)

        # PW_RENDERFULLCONTENT(2)对Chromium/DirectX渲染的窗口有效，失败则回退标准模式
        if not ctypes.windll.user32.PrintWindow(hwnd, save_dc.GetSafeHdc(), 2):
            ctypes.windll.user32.PrintWindow(hwnd, save_dc.GetSafeHdc(), 0)

        bmp_info = bitmap.GetInfo()
        # 必须传True才返回bytes，不传返回tuple会导致np.frombuffer报错
        bmp_str = bitmap.GetBitmapBits(True)
        img = np.frombuffer(bmp_str, dtype=np.uint8).reshape(
            bmp_info["bmHeight"], bmp_info["bmWidth"], 4
        )
        img = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)

        win32gui.DeleteObject(bitmap.GetHandle())
        save_dc.DeleteDC()
        mfc_dc.DeleteDC()
        win32gui.ReleaseDC(hwnd, hwnd_dc)

        # 裁剪出客户区，保证截图左上角与self.game_window坐标一致
        client_rect = win32gui.GetClientRect(hwnd)
        client_origin = win32gui.ClientToScreen(hwnd, (0, 0))
        client_w, client_h = client_rect[2], client_rect[3]
        off_x = client_origin[0] - left
        off_y = client_origin[1] - top

        # DPI缩放时位图像素与窗口逻辑尺寸不一致，按比例换算裁剪区域
        scale_x = bmp_info["bmWidth"] / win_w
        scale_y = bmp_info["bmHeight"] / win_h
        x0 = int(off_x * scale_x)
        y0 = int(off_y * scale_y)
        x1 = int((off_x + client_w) * scale_x)
        y1 = int((off_y + client_h) * scale_y)
        img = img[max(0, y0):y1, max(0, x0):x1]

        # 统一缩放到客户区逻辑尺寸，保证模板匹配坐标系一致
        if img.shape[1] != client_w or img.shape[0] != client_h:
            img = cv2.resize(img, (client_w, client_h))

        # 刷新窗口位置（窗口被拖动后依然有效）
        self.game_window = (client_origin[0], client_origin[1], client_w, client_h)
        return img

    def find_game_window(self):
        """查找游戏窗口（后台模式下不抢占前台）"""
        hwnd = self._get_hwnd()
        if hwnd:
            if not self.background_mode:
                win32gui.SetForegroundWindow(hwnd)
            left, top, right, bottom = win32gui.GetWindowRect(hwnd)
            self.game_window = (left, top, right - left, bottom - top)
            print(f"找到游戏窗口: {self.game_window}")
            return True
        else:
            print("未找到游戏窗口")
            return False

    def resize_game_window(self, width=542, height=1010):
        """调整游戏窗口大小，并确保窗口处于屏幕内可见位置（便于PrintWindow截图）"""
        hwnd = win32gui.FindWindow(None, self.game_title)
        if hwnd:
            # 最小化窗口无法调整，先恢复
            if win32gui.IsIconic(hwnd):
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                time.sleep(0.3)
            # 获取当前窗口位置
            left, top, right, bottom = win32gui.GetWindowRect(hwnd)
            # 计算窗口边框和标题栏的大小
            # 先获取客户区大小
            client_rect = win32gui.GetClientRect(hwnd)
            client_width = client_rect[2] - client_rect[0]
            client_height = client_rect[3] - client_rect[1]
            # 计算边框和标题栏的总大小
            border_width = (right - left) - client_width
            border_height = (bottom - top) - client_height
            # 计算需要设置的窗口总大小
            window_width = width + border_width
            window_height = height + border_height
            # 若窗口被移出屏幕外，放回屏幕内可见位置（主屏居中偏右上）
            vx = win32api.GetSystemMetrics(76)
            vy = win32api.GetSystemMetrics(77)
            vw = win32api.GetSystemMetrics(78)
            vh = win32api.GetSystemMetrics(79)
            if right <= vx or bottom <= vy or left >= vx + vw or top >= vy + vh:
                left = vx + vw - window_width - 40
                top = vy + 40
                if left < vx:
                    left = vx
            # 设置新的窗口大小
            win32gui.MoveWindow(hwnd, left, top, window_width, window_height, True)
            # 更新游戏窗口信息
            self.game_window = (left, top, width, height)
            print(f"游戏窗口已调整为: {self.game_window}")
            return True
        else:
            print("未找到游戏窗口，无法调整大小")
            return False

    def find_fullscreen_window(self):
        """查找全屏幕窗口"""
        # 使用pyautogui获取屏幕尺寸，更简单可靠
        try:
            # 获取主屏幕尺寸
            width, height = pyautogui.size()
            left, top = 0, 0
            self.game_window = (left, top, width, height)
            print(f"全屏幕窗口: {self.game_window}")
            return True
        except Exception as e:
            print(f"获取屏幕尺寸时出错: {e}")
            # 如果pyautogui失败，尝试使用win32gui的基本方法
            try:
                width = win32gui.GetSystemMetrics(0)  # SM_CXSCREEN
                height = win32gui.GetSystemMetrics(1)  # SM_CYSCREEN
                left, top = 0, 0
                self.game_window = (left, top, width, height)
                print(f"使用备用方法获取全屏幕窗口: {self.game_window}")
                return True
            except Exception as e2:
                print(f"备用方法也失败: {e2}")
                return False

    def take_screenshot(self, force_new=False):
        """截取游戏窗口画面（支持缓存，返回BGR格式）"""
        now = time.time()

        if not force_new and self._screenshot_bgr is not None:
            if (now - self._screenshot_time) <= self._screenshot_ttl:
                return self._screenshot_bgr

        if not self.game_window:
            if not self.find_game_window():
                return None

        if self.background_mode and self._get_hwnd():
            # 后台模式：PrintWindow截图，窗口可被遮挡
            img = self._capture_window()
        else:
            screenshot = pyautogui.screenshot(region=self.game_window)
            img = cv2.cvtColor(np.array(screenshot), cv2.COLOR_RGB2BGR)

        if img is None:
            return None
        self._screenshot_bgr = img
        self._screenshot_time = now
        return self._screenshot_bgr

    def save_screenshot(self, filename=None):
        """保存截图"""
        img = self.take_screenshot()
        if img is not None:
            if not filename:
                filename = f"{self.screenshot_dir}/{int(time.time())}.png"
            cv2.imwrite(filename, img)
            print(f"截图已保存: {filename}")
            return filename
        return None

    def find_template(self, template_name, threshold=0.8):
        """在游戏窗口中查找模板图像"""
        if template_name in self.template_cache:
            template = self.template_cache[template_name]
        else:
            template_path = os.path.join(self.template_dir, template_name)
            template = cv2.imread(template_path)
            if template is not None:
                self.template_cache[template_name] = template

        if template is None:
            print(f"无法加载模板: {template_name}")
            return None

        img = self.take_screenshot()
        if img is None:
            return None

        t_h, t_w = template.shape[:2]
        i_h, i_w = img.shape[:2]
        # 模板比截图还大：OpenCV matchTemplate要求模板<=截图，否则断言崩溃。
        # 这种情况说明窗口太小或模板异常（如battling-stop.png是2160x2160大图），直接跳过。
        if t_h > i_h or t_w > i_w:
            return None

        result = cv2.matchTemplate(img, template, cv2.TM_CCOEFF_NORMED)
        min_val, max_val, min_loc, max_loc = cv2.minMaxLoc(result)

        # 匹配诊断日志（节流）：命中 或 接近阈值时记录，避免逐帧刷屏
        if self.match_log_enabled:
            self._log_match_score(template_name, max_val, threshold, max_loc)

        if max_val >= threshold:
            h, w = template.shape[:2]
            center_x = self.game_window[0] + max_loc[0] + w // 2
            center_y = self.game_window[1] + max_loc[1] + h // 2
            return (center_x, center_y)

        return None

    def _log_match_score(self, name, score, threshold, max_loc):
        """节流输出匹配分数：只记录命中或接近阈值的情况。
        命中状态翻转或分数档位变化时立即记录，否则按 MATCH_LOG_INTERVAL 间隔记录。"""
        if not self.match_log_enabled:
            return
        hit = score >= threshold
        near = (not hit) and score >= threshold - self.MATCH_LOG_NEAR
        if not (hit or near):
            return

        # 命中/未命中 + 分数档位(0.05一档)，作为节流的状态标识
        bucket = (hit, round(score * 20))
        now = time.time()
        prev = self._match_log_state.get(name)
        # 单步调试时不节流：用户每点一次"下一步"就要看到完整匹配分数，
        # 否则相同分数档位被压制，看起来像"卡住没反应"。
        if prev and prev[0] == bucket and now - prev[1] < self.MATCH_LOG_INTERVAL and not self.debug_mode:
            return
        self._match_log_state[name] = (bucket, now)

        tag = "命中" if hit else "接近阈值(未点击)"
        print(f"[匹配] {name:<22s} {score:.3f}/{threshold:.2f} {tag} @({max_loc[0]},{max_loc[1]})")

    def find_all_templates(self, template_name, threshold=0.8):
        """在游戏窗口中查找所有匹配的模板图像位置"""
        if template_name in self.template_cache:
            template = self.template_cache[template_name]
        else:
            template_path = os.path.join(self.template_dir, template_name)
            template = cv2.imread(template_path)
            if template is not None:
                self.template_cache[template_name] = template

        if template is None:
            print(f"无法加载模板: {template_name}")
            return []

        img = self.take_screenshot()
        if img is None:
            return []

        t_h, t_w = template.shape[:2]
        i_h, i_w = img.shape[:2]
        # 模板比截图还大：OpenCV matchTemplate要求模板<=截图，否则断言崩溃，直接跳过
        if t_h > i_h or t_w > i_w:
            return []

        result = cv2.matchTemplate(img, template, cv2.TM_CCOEFF_NORMED)

        locations = np.where(result >= threshold)
        matches = []

        h, w = template.shape[:2]

        for pt in zip(*locations[::-1]):
            center_x = self.game_window[0] + pt[0] + w // 2
            center_y = self.game_window[1] + pt[1] + h // 2
            matches.append((center_x, center_y))

        return matches

    def click(self, x, y, duration=0.2, human_like=True):
        """模拟鼠标点击"""
        if human_like:
            # 添加随机偏移，模拟人类点击
            x += random.randint(-5, 5)
            y += random.randint(-5, 5)

        if self.background_mode and self._get_hwnd():
            # 后台模式：发送鼠标消息，不占用真实鼠标
            self._post_click(x, y)
            return

        duration += random.uniform(-0.1, 0.1)
        duration = max(0.1, duration)
        pyautogui.moveTo(x, y, duration=duration)
        pyautogui.click()
        # print(f"点击位置: ({x}, {y})")

    def click_fast(self, x, y):
        """快速点击（后台模式发送消息，前台模式直接发送鼠标事件）"""
        if self.background_mode and self._get_hwnd():
            self._post_click(x, y, press_delay=0.02)
            return
        win32api.SetCursorPos((int(x), int(y)))
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)

    def click_fast_batch(self, positions, interval=0.05):
        """批量后台点击多个位置（仅 PostMessage，不移动真实鼠标）"""
        for x, y in positions:
            self.click_fast(x, y)
            time.sleep(interval)

    def swipe_up(self, distance=200, duration=0.3):
        """在游戏窗口中间位置按住鼠标向上滑动，页面上划指定像素"""
        if not self.game_window:
            if not self.find_game_window():
                return
        left, top, width, height = self.game_window
        center_x = left + width // 2
        start_y = top + height // 2
        end_y = start_y - distance

        if self.background_mode and self._get_hwnd():
            # 后台模式：发送拖动消息
            self._post_drag(center_x, start_y, center_x, end_y, duration)
            time.sleep(1.5)
            return

        pyautogui.moveTo(center_x, start_y, duration=0.1)
        pyautogui.mouseDown()
        # ease-out减速收尾，终点停顿后再松开，避免页面惯性滑动
        pyautogui.moveTo(center_x, end_y, duration=duration, tween=pyautogui.easeOutQuad)
        time.sleep(0.15)
        pyautogui.mouseUp()
        time.sleep(1.5)

    def press_key(self, key, presses=1, interval=0.1, human_like=True):
        """模拟按键"""
        if human_like:
            interval += random.uniform(-0.05, 0.05)
            interval = max(0.05, interval)

        if self.background_mode and self._get_hwnd():
            # 后台模式：向窗口发送键盘消息
            self._post_key(key, presses, interval)
            return

        pyautogui.press(key, presses=presses, interval=interval)
        print(f"按下按键: {key}")

    def click_template(self, template_name, sleep_after=0.05):
        """查找并点击单个模板"""
        pos = self.find_template(template_name)
        if pos:
            self.click(*pos)
            if sleep_after > 0:
                time.sleep(sleep_after)
            return True
        return False

    def click_first_template(self, template_names, sleep_after=0.05):
        """查找并点击第一个匹配的模板"""
        for template_name in template_names:
            pos = self.find_template(template_name)
            if pos:
                self.click(*pos)
                if sleep_after > 0:
                    time.sleep(sleep_after)
                return True
        return False

    def find_click_receive(self):
        """判断能否点击领取按钮"""
        self.click_template("receive.png")

    def find_click_im(self):
        """判断能否点击环球页面"""
        self.click_template("im.png")

    def find_click_continue(self):
        """判断能否点击继续按钮"""
        self.click_template("click-continue.png")

    def find_team_up(self):
        """判断能否发现队伍页面"""
        return self.find_template("recruitment-1.png")

    def _recruit_y_keys(self, positions):
        b = self.RECRUIT_Y_BUCKET
        return {int(round(y / b)) for _, y in positions}

    def _recruit_note_new(self, positions):
        """上一帧没有、本帧新出现的环球位 → 记一次新队机会（已在抢窗口内则不重复计）"""
        keys = self._recruit_y_keys(positions)
        new_keys = keys - self._recruit_prev_ys
        self._recruit_prev_ys = keys
        if not new_keys:
            return
        if self._recruit_pending_since is not None:
            return
        self._recruit_pending_since = time.time()
        self._recruit_new += 1
        print(
            f"[抢队] 新队伍 #{self._recruit_new} "
            f"(间隔 {self.recruit_click_interval:.2f}s)"
        )

    def _recruit_settle(self, in_team):
        """结算进行中的新队窗口：进队=成功，超时=失败"""
        if self._recruit_pending_since is None:
            return False
        now = time.time()
        if in_team:
            self._recruit_ok += 1
            self._recruit_pending_since = None
            self._print_recruit_stats("抢中")
            return True
        if now - self._recruit_pending_since >= self.RECRUIT_RACE_SEC:
            self._recruit_fail += 1
            self._recruit_pending_since = None
            self._print_recruit_stats("未中")
        return False

    def _print_recruit_stats(self, event=None):
        n = self._recruit_new
        ok = self._recruit_ok
        rate = (100.0 * ok / n) if n else 0.0
        prefix = f"[抢队] {event} · " if event else "[抢队] "
        print(
            f"{prefix}新队 {n} · 抢中 {ok} · "
            f"未中 {self._recruit_fail} · 成功率 {rate:.0f}% · "
            f"间隔 {self.recruit_click_interval:.2f}s"
        )

    def find_click_recruitment(self):
        while True and self.running:
            """判断能否点击招募页面"""
            team_up = self.find_team_up()
            if not team_up:
                recruitments = ["recruitment.png"]
                in_recruitment = False
                for recruitment in recruitments:
                    xy = self.find_template(recruitment)
                    if xy:
                        self.click(*xy)
                        in_recruitment = True
                        break
                if not in_recruitment:
                    print("未找到招募页面")
                    break
            self.find_click_reconnection()
            for i in range(20):
                if not self.running:
                    break
                try:
                    huanqiu_positions = self.find_all_templates("huanqiu.png")
                    if huanqiu_positions:
                        self._recruit_note_new(huanqiu_positions)
                        # 使用快速点击方法点击所有找到的环球按钮
                        positions = []
                        for pos in huanqiu_positions:
                            pos = (pos[0] + 150, pos[1])
                            positions.append(pos)
                        if positions:
                            # 倒序连点，间隔可配，优先抢队速度
                            positions.reverse()
                            self.click_fast_batch(
                                positions, interval=self.recruit_click_interval
                            )
                        if self._recruit_settle(self.find_in_huanqiu_team()):
                            return
                    else:
                        self._recruit_prev_ys = set()
                        if self._recruit_settle(self.find_in_huanqiu_team()):
                            return
                        leave_button = self.find_leave_button()
                        if leave_button:
                            if not self.find_in_huanqiu_team():
                                self.click(*leave_button)
                                time.sleep(0.04)
                                self.find_click_sure()
                        # 不打深渊
                        if self.find_double_abyss():
                            self.find_click_dont_battle_return()
                    self._recruit_settle(False)
                except Exception:
                    # 兜底：单次异常不影响主循环，节流打印
                    now = time.time()
                    if now - getattr(self, "_last_recruit_log", 0) >= 5:
                        self._last_recruit_log = now
                        print("查找环球按钮时出错")

    def find_in_huanqiu_team(self):
        """是否在环球队伍"""
        huanqiu_team = self.find_template("in-huanqiu-team.png")
        if huanqiu_team:
            return True
        return False

    def find_click_home_close(self):
        """判断能否点击关闭按钮"""
        self.click_first_template(
            [
                "home-close.png",
                "home-close-1.png",
                "home-close-2-text.png",
                "home-close-2.png",
            ]
        )

    def find_click_close(self):
        """判断能否点击关闭按钮"""
        return self.click_first_template(
            [
                "close.png",
                "auto-close.png",
                "battling-4.png",
            ]
        )

    def find_click_reconnection(self):
        """判断能否点击重新连接按钮"""
        self.click_template("reconnection.png")

    def find_huanqiu(self):
        """判断能否发现环球按钮"""
        return self.find_template("huanqiu.png")

    def find_click_start_button(self):
        """判断能否点击战斗按钮"""
        return self.click_first_template(["battle.png", "battle-1.png"])

    def find_click_sure(self):
        """判断能否点击确定按钮"""
        self.click_template("sure.png")

    def find_click_auto_close(self):
        """判断能否点击继续战斗按钮"""
        self.click_template("auto-close.png")

    def _card_offsets_4(self):
        """4选2：卡位图标中心相对「选择技能」锚点（542x1010 实测）。"""
        return [(-195, 151), (-65, 151), (65, 151), (195, 151)]

    def _card_offsets_3_icon(self):
        """3选1：图标中心相对锚点（用于 ROI 匹配优先级）。"""
        return [(-150, 278), (0, 278), (150, 278)]

    def _card_offsets_3_click(self):
        """3选1：点击点相对锚点（文字区更易点中，元素实测 +345）。"""
        return [(-150, 345), (0, 345), (150, 345)]

    def _abyss_selection_count(self, img, anchor_cx_client, anchor_cy_client):
        """用底部确定按钮上的「0/2」「1/2」「2/2」文字判断已选张数。

        金框=技能强化特效，不能当选中态。
        注意：没有 0of2 时，「0/2」会更像「2/2」(实测 0.93) 而被误判已满，
        从而对灰掉的确定按钮空点卡住——必须三态一起比。
        """
        ax, ay = int(anchor_cx_client), int(anchor_cy_client)
        bx, by = ax - 8, ay + 410
        h_img, w_img = img.shape[:2]
        x1, y1 = max(0, bx - 130), max(0, by - 40)
        x2, y2 = min(w_img, bx + 130), min(h_img, by + 40)
        roi = img[y1:y2, x1:x2]
        if roi.size == 0:
            return 0

        scores = {}
        for n, name in (
            (0, "abyss-skill-0of2.png"),
            (1, "abyss-skill-1of2.png"),
            (2, "abyss-skill-2of2.png"),
        ):
            if name in self.template_cache:
                tmpl = self.template_cache[name]
            else:
                tmpl = cv2.imread(os.path.join(self.template_dir, name))
                if tmpl is not None:
                    self.template_cache[name] = tmpl
            if tmpl is None:
                scores[n] = 0.0
                continue
            th, tw = tmpl.shape[:2]
            rh, rw = roi.shape[:2]
            if th > rh or tw > rw:
                scores[n] = 0.0
                continue
            res = cv2.matchTemplate(roi, tmpl, cv2.TM_CCOEFF_NORMED)
            scores[n] = float(cv2.minMaxLoc(res)[1])

        best_n, best_s = max(scores.items(), key=lambda kv: kv[1])
        if best_s < 0.90:
            return 0
        return best_n

    # 技能模板取自更大的卡面特写(图标直径≈85px)，而选技能界面的卡图标≈60px。
    # 实测模板缩放 0.70~0.75 时匹配分 0.93~0.98，1.0 原尺寸则全部<0.01(根本匹配不到)。
    SKILL_TPL_SCALE = 0.72

    def _match_templates_in_roi(self, img, cx, cy, templates, threshold=0.65):
        """仅在卡位 ROI 内匹配技能模板，避免全屏误匹配战斗背景。

        关键：模板需按 SKILL_TPL_SCALE 缩放后才能匹配当前界面的卡图标，
        否则原尺寸(136x128)大于ROI且图标尺度不符，全部漏匹配 -> 优先级失效、
        退化成从左到右固定点。缩放后的模板缓存复用，避免每卡重复 resize。
        """
        h_img, w_img = img.shape[:2]
        x1, y1 = max(0, cx - 65), max(0, cy - 80)
        x2, y2 = min(w_img, cx + 65), min(h_img, cy + 80)
        roi = img[y1:y2, x1:x2]
        if roi.size == 0:
            return 0.0
        best = 0.0
        for name in templates:
            if not name:
                continue
            if not name.endswith(".png"):
                name = f"{name}.png"
            cache_key = f"scaled:{name}"
            if cache_key in self.template_cache:
                tmpl = self.template_cache[cache_key]
            else:
                raw = cv2.imread(os.path.join(self.template_dir, name))
                if raw is None:
                    continue
                tmpl = cv2.resize(
                    raw, None, fx=self.SKILL_TPL_SCALE, fy=self.SKILL_TPL_SCALE,
                    interpolation=cv2.INTER_AREA,
                )
                self.template_cache[cache_key] = tmpl
            th, tw = tmpl.shape[:2]
            rh, rw = roi.shape[:2]
            if th > rh or tw > rw:
                continue
            res = cv2.matchTemplate(roi, tmpl, cv2.TM_CCOEFF_NORMED)
            score = float(cv2.minMaxLoc(res)[1])
            if score > best:
                best = score
        return best if best >= threshold else 0.0

    def _preferred_skill_slots(self, img, client_centers):
        """全模式共用：按 优先技能 → SKILL_LIST → 左到右 给卡位排序。"""
        groups = []
        for templates in self.priority_skills:
            if templates:
                groups.append(templates)
        for skill in SKILL_LIST:
            groups.append(skill["template"])

        slot_rank = []
        for slot, (cx, cy) in enumerate(client_centers):
            rank = 999
            score = 0.0
            for prio_idx, templates in enumerate(groups):
                s = self._match_templates_in_roi(img, cx, cy, templates)
                if s > 0:
                    rank = prio_idx
                    score = s
                    break
            slot_rank.append((rank, -score, slot))
        slot_rank.sort()
        return [slot for _, __, slot in slot_rank]

    def _match_choose_skill_client(self, img, threshold=0.8):
        """在截图（客户区坐标系）上找「选择技能」锚点中心，返回 (cx,cy) 或 None。"""
        best = None
        for name in ("choose-skill.png", "choose-skill-1.png"):
            if name in self.template_cache:
                tmpl = self.template_cache[name]
            else:
                tmpl = cv2.imread(os.path.join(self.template_dir, name))
                if tmpl is not None:
                    self.template_cache[name] = tmpl
            if tmpl is None:
                continue
            th, tw = tmpl.shape[:2]
            ih, iw = img.shape[:2]
            if th > ih or tw > iw:
                continue
            res = cv2.matchTemplate(img, tmpl, cv2.TM_CCOEFF_NORMED)
            max_val = float(cv2.minMaxLoc(res)[1])
            max_loc = cv2.minMaxLoc(res)[3]
            if max_val >= threshold and (best is None or max_val > best[0]):
                best = (max_val, max_loc[0] + tw // 2, max_loc[1] + th // 2)
        if not best:
            return None
        return (best[1], best[2])

    def _click_client(self, cx, cy, press_delay=0.12):
        """客户区坐标 → 当前窗口实时屏幕坐标再后台点击（避免藏窗后仍用旧屏幕坐标）。"""
        hwnd = self._get_hwnd()
        if not hwnd:
            return
        sx, sy = win32gui.ClientToScreen(hwnd, (int(cx), int(cy)))
        if self.background_mode:
            self._post_click(sx, sy, press_delay=press_delay)
        else:
            self.click(sx, sy, human_like=False)

    def find_click_skill_abyss(self, anchor=None, tag="深渊", choose_threshold=None):
        """深渊/元素试炼共用的 4选2 选技能（按技能优先级选卡）。

        已选张数看底部按钮「0/2」「1/2」「2/2」（金框=强化特效不能当选中）。
        点卡后复核张数：增加=选中；减少=点到已选被取消，点回并跳过。
        全程客户区坐标 + 点击时 ClientToScreen，后台 PostMessage，不挪真实鼠标。

        anchor: 可选的「选择技能」锚点(屏幕坐标)，匹配失败时回退换算。
        choose_threshold: 标题匹配阈值；默认深渊0.8、传入anchor时0.55（元素火焰帧）。
        tag: 日志前缀，区分深渊/元素。
        """
        img = self.take_screenshot(force_new=True)
        if img is None:
            return None

        if choose_threshold is None:
            choose_threshold = 0.55 if anchor is not None else 0.8
        client_anchor = self._match_choose_skill_client(img, threshold=choose_threshold)
        if not client_anchor and anchor and self.game_window:
            client_anchor = (
                int(anchor[0] - self.game_window[0]),
                int(anchor[1] - self.game_window[1]),
            )
        if not client_anchor:
            return None

        ax, ay = client_anchor
        client_centers = [(ax + dx, ay + dy) for dx, dy in self._card_offsets_4()]
        count = self._abyss_selection_count(img, ax, ay)
        preferred = self._preferred_skill_slots(img, client_centers)
        print(f"[{tag}选技能] 按钮张数={count}/2 优先顺序={preferred}")

        confirm_cx, confirm_cy = ax - 8, ay + 410

        if count >= 2:
            self._click_client(confirm_cx, confirm_cy)
            time.sleep(0.4)
            print(f"[{tag}选技能] 已满2张，直接确定 客户区({confirm_cx},{confirm_cy})")
            return (ax, ay)

        picked = []
        for slot in preferred:
            if count >= 2:
                break
            cx, cy = client_centers[slot]
            before = count
            self._click_client(cx, cy, press_delay=0.12)
            time.sleep(0.55)
            img = self.take_screenshot(force_new=True)
            if img is None:
                break
            # 锚点可能随动画微抖，张数仍按原 ay 附近按钮区域读
            count = self._abyss_selection_count(img, ax, ay)
            if count > before:
                picked.append((slot, "选中", count))
                print(f"[{tag}选技能] 卡位{slot}选中 → {count}/2")
            elif count < before:
                self._click_client(cx, cy, press_delay=0.12)
                time.sleep(0.55)
                img = self.take_screenshot(force_new=True)
                if img is not None:
                    count = self._abyss_selection_count(img, ax, ay)
                picked.append((slot, "已选跳过", count))
                print(f"[{tag}选技能] 卡位{slot}本已选中，恢复后跳过 → {count}/2")
            else:
                picked.append((slot, "无效果", count))
                print(f"[{tag}选技能] 卡位{slot}点击无效果 → {count}/2")

        if count >= 2:
            time.sleep(0.2)
            self._click_client(confirm_cx, confirm_cy)
            time.sleep(0.4)
            print(
                f"[{tag}选技能] 凑满2张后确定 {picked} 客户区({confirm_cx},{confirm_cy})"
            )
        else:
            print(f"[{tag}选技能] 本轮仍不足2张({count})，下轮再试 {picked}")
        return (ax, ay)

    def find_click_skill_pick_one(self, tag="选技能", choose_threshold=0.8):
        """3选1 统一逻辑：卡位 ROI 按技能优先级排序，点排序第一的卡（点即生效）。

        优先级：GUI 优先技能 → SKILL_LIST → 左到右。全未命中时点左起第一张兜底。
        """
        img = self.take_screenshot(force_new=True)
        if img is None:
            return None
        client_anchor = self._match_choose_skill_client(img, threshold=choose_threshold)
        if not client_anchor:
            return None
        ax, ay = client_anchor
        icon_centers = [(ax + dx, ay + dy) for dx, dy in self._card_offsets_3_icon()]
        click_centers = [(ax + dx, ay + dy) for dx, dy in self._card_offsets_3_click()]
        preferred = self._preferred_skill_slots(img, icon_centers)
        slot = preferred[0]
        cx, cy = click_centers[slot]
        self._click_client(cx, cy, press_delay=0.12)
        time.sleep(0.3)
        print(f"[{tag}] 3选1 优先顺序={preferred} → 点卡位{slot} 客户区({cx},{cy})")
        return (ax, ay)

    def find_click_skill_element(self):
        """元素试炼选技能：前5轮4选2、第6轮起3选1；一律按技能优先级。
        两种布局靠"选择技能"标题中心 y 区分（客户区）：
          4选2 标题中心 y≈342；3选1 标题中心 y≈205
        标题模板火焰动画帧会导致匹配分波动，阈值取 0.55。
        """
        img = self.take_screenshot(force_new=True)
        if img is None:
            return None
        client_anchor = self._match_choose_skill_client(img, threshold=0.55)
        if not client_anchor:
            return None
        ax, ay = client_anchor
        if ay > 280:
            # 4选2：与深渊同一套优先级+按钮张数逻辑（火焰标题用 0.55）
            return self.find_click_skill_abyss(tag="元素", choose_threshold=0.55)
        return self.find_click_skill_pick_one(tag="元素", choose_threshold=0.55)

    def find_click_skill(self):
        """选技能总入口：所有模式统一按技能优先级选卡。"""
        self.find_click_think_tank()
        if self.mode == 4:
            return self.find_click_skill_abyss(tag="深渊")
        if self.mode == 5:
            return self.find_click_skill_element()
        # 主线/环球：3选1，卡位 ROI + 优先级（不再全屏盲匹配、不再固定点中间）
        return self.find_click_skill_pick_one(tag="选技能", choose_threshold=0.8)

    def find_battling(self):
        """判断是否在战斗中"""
        battles = [
            "battling.png",
            "battling-3.png",
            "battling-4.png",
            "battling-5.png",
            "auto-close.png",
            "choose-skill.png",
            "choose-skill-1.png",
            "open-skills.png",
        ]
        for battle in battles:
            xy = self.find_template(battle)
            if xy:
                if not self.current_battle_time:
                    self.current_battle_time = time.time()
                return xy
        return None

    def find_click_dont_battle_return(self):
        """判断能否点击返回按钮"""
        self.click_template("return-1.png")

    def find_click_return(self):
        """判断能否点击返回主界面"""
        return_button = self.find_template("return.png")
        if return_button:
            self.current_battle_time = 0
            if self.mode not in [2, 3]:
                self.battle_count_add()

            self.click(*return_button)
            print(f"战斗次数: {self.battle_count}")
            time.sleep(0.1)

    def battle_count_add(self):
        """战斗次数+1"""
        now = time.time()
        if now - self.last_battle_count_time >= 5:
            self.battle_count += 1
            self.last_battle_count_time = now
            if self.on_battle_count_changed:
                self.on_battle_count_changed(self.battle_count)

    def find_stop(self):
        """找到停止按钮"""
        stop = self.find_template("battling.png")
        if stop:
            return stop
        return None

    def force_click_stop(self):
        """强制点击停止按钮"""
        (left, top, width, height) = self.game_window
        stop_left = left + 50
        stop_top = top + 85
        stop = (stop_left, stop_top)
        print(f"强制点击停止按钮: {stop}")
        self.click(*stop)
        
    def find_click_exit(self):
        """判断能否点击退出按钮"""
        self.click_template("exit.png")

    def find_click_card(self):
        """判断能否点击卡关按钮"""
        card1 = self.find_template("card-normal-1.png")
        if card1:
            self.click(*card1)
            time.sleep(0.2)
        card = self.find_template("card-normal.png")
        if card:
            self.click(*card)
            time.sleep(0.2)
            card = self.find_template("card-start.png")
            if card:
                self.click(*card)
                time.sleep(0.1)

    def find_click_orange_start_game(self):
        """判断能否点击橘子开始游戏按钮"""
        self.click_template("orange-start.png")

    def find_expedition_team(self):
        """判断能否在远征队伍中"""
        expedition_team_icons = ["expedition-team.png", "expedition-team-2.png"]
        for icon in expedition_team_icons:
            expedition_team = self.find_template(icon)
            if expedition_team:
                return True
        return False

    def find_click_base(self):
        """判断能否点击基地按钮"""
        self.click_first_template(["base.png", "base-2.png"])

    def find_base(self):
        """判断能否点击基地按钮"""
        bases_icon = ["base.png", "base-2.png"]
        for icon in bases_icon:
            base = self.find_template(icon)
            if base:
                return base
        return None

    def find_click_experience(self):
        """判断能否点击历练按钮"""
        self.click_template("experience.png")

    # ==================== 深渊挑战模式 (mode=4) ====================
    # 导航路径: 首页 -> 基地 -> 历练大厅 -> 深渊挑战页 -> 挑战 -> 战斗 -> 结算 -> 回深渊页
    # 全部模板基于 542x1010 客户区截图，缩放比例一致。
    # 历练大厅有4个同形"挑战"按钮，必须用"深渊挑战"标题做锚点定位，不能全局找挑战。

    def find_abyss_title(self):
        """在历练大厅定位"深渊挑战"标题条(唯一锚点)，返回其左上角坐标"""
        return self.find_template("abyss-title.png")

    def find_click_abyss_entry(self):
        """在历练大厅：找到深渊挑战标题锚点，点击其卡片内的"挑战"按钮。

        注意 find_template() 返回的是模板"中心点"屏幕坐标，不是左上角，
        所以偏移必须以标题中心为基准，不能拿左上角的偏移来加。
        实测(542×1010)：标题中心(109,262)、挑战按钮中心(370,407) → 偏移(+261,+145)。
        """
        title = self.find_abyss_title()
        if not title:
            return False
        # 挑战按钮中心 = 标题中心 + 固定偏移（实测，相对中心而非左上角）
        btn_x = title[0] + 261
        btn_y = title[1] + 145
        self.click(btn_x, btn_y)
        time.sleep(0.3)
        print(f"[深渊] 点击入口挑战按钮 ({btn_x},{btn_y})")
        return True

    def find_click_abyss_confirm(self):
        """点击"深渊怪物强化"弹窗的确定按钮(阈值0.9，避免误点选技能界面的确定)"""
        pos = self.find_template("abyss-confirm.png", threshold=0.9)
        if not pos:
            return False
        self.click(*pos)
        time.sleep(0.3)
        return True

    def find_abyss_page(self):
        """判断是否已进入深渊挑战页面：用顶部"当前层数"固定锚点判定。
        该锚点在深渊页=1.0，其他页面约0.5，不随挑战次数变化，
        因此与"挑战按钮是否可点"解耦，次数用完时仍能正确识别在深渊页。
        """
        return self.find_template("abyss-page-anchor.png", threshold=0.85)

    def find_click_abyss_challenge(self):
        """在深渊挑战页点击"挑战"按钮。
        次数用完后按钮变灰，模板匹配分数会低于0.85 -> 返回False 表示次数耗尽。
        """
        pos = self.find_template("abyss-page-challenge.png", threshold=0.9)
        if not pos:
            return False
        self.click(*pos)
        time.sleep(0.3)
        return True

    # ==================== 元素试炼模式 (mode=5) ====================
    # 导航路径: 首页 -> 基地 -> 历练大厅 ->(下滑到底) 元素试炼"挑战"
    #           -> 元素试炼塔页 -> 点核心塔 -> 核心试炼详情页
    #           -> "挑战" -> "开始游戏"弹窗 -> 战斗(选技能) -> 结算页
    #           -> "下一关" -> 回核心试炼详情页(层数+1/次数-1) -> 循环
    # 每天10次机会，次数用完即停，绝不自动点"+"。

    def find_element_title(self):
        """历练大厅底部"元素试炼"标题条锚点。
        阈值0.9：真标题实测1.000；顶部"深渊挑战"橙色标题误匹配仅0.796，
        阈值0.85会误命中并按偏移点到深渊卡片(跑偏模式)，故提到0.9隔离。
        """
        return self.find_template("element-title.png", threshold=0.9)

    def find_click_element_entry(self):
        """历练大厅：定位"元素试炼"标题，点其卡片内"挑战"按钮。
        实测(542x1010)：标题中心(106,711)、挑战按钮中心(369,854) -> 偏移(+263,+143)。
        """
        title = self.find_element_title()
        if not title:
            return False
        btn_x, btn_y = title[0] + 263, title[1] + 143
        self.click(btn_x, btn_y)
        time.sleep(0.3)
        print(f"[元素] 点击入口挑战按钮 ({btn_x},{btn_y})")
        return True

    def scroll_lobby_down(self):
        """历练大厅向下滑动一屏（从底部往上划），用于露出底部的元素试炼卡片。

        坐标坑：_post_drag/_post_mouse 期望【屏幕坐标】并内部做 ScreenToClient；
        客户端区目标点(271,780)->(271,300)必须加 game_window 原点偏移再传入。
        直接传客户区坐标会被二次扣减原点变成负X，消息落到窗口外，滑动无效。
        """
        if not self.game_window:
            if not self.find_game_window():
                return
        ox, oy = self.game_window[0], self.game_window[1]
        self._post_drag(ox + 271, oy + 780, ox + 271, oy + 300, duration=0.4)
        time.sleep(0.4)
        print(f"[元素] 历练大厅下滑一次 (计数 {self.element_scroll_count}/5)")

    def find_element_tower_page(self):
        """判断是否进入元素试炼塔页（顶部"元素试炼"大字锚点）"""
        return self.find_template("element-tower.png", threshold=0.85)

    def find_click_element_core_tower(self):
        """元素试炼塔页：点中央核心塔的发光核心，进入核心试炼详情页。
        实测核心塔发光核心中心客户区(320,358)。
        """
        gw = self.game_window
        x = gw[0] + 320 if gw else 320
        y = gw[1] + 358 if gw else 358
        self.click(x, y)
        time.sleep(0.5)
        print(f"[元素] 点击核心塔 ({x},{y})")
        return True

    def find_element_core_page(self):
        """判断是否进入核心试炼详情页——只用"挑战"按钮做判据。
        曾把标题 element-core-page.png(阈值0.75)作辅判据，但历练大厅顶部
        "跨服战争争霸"卡片会误匹配到0.936，导致在大厅里误判"在详情页"，
        随后找不到挑战按钮被当成"次数用完"提前退出(实际次数9/10)。
        挑战按钮实测：真详情页1.000，大厅元素卡片0.65，阈值0.9可干净区分。
        """
        return bool(self.find_template("element-core-btn.png", threshold=0.9))

    def find_element_challenge_greyed(self):
        """次数用完后挑战按钮变灰：用较低阈值确认"详情页还在、但按钮已灰"。
        真页橙色按钮1.000；灰色按钮分数会掉到0.9以下；大厅元素卡片仅0.65。
        取阈值0.78：能覆盖灰按钮，又不会把大厅卡片(0.65)误判成灰按钮。
        """
        return bool(self.find_template("element-core-btn.png", threshold=0.78))

    def find_click_element_challenge(self):
        """核心试炼详情页：点"挑战"按钮（模板定位）。
        次数用完时按钮变灰/消失 -> 匹配失败返回False。
        """
        pos = self.find_template("element-core-btn.png", threshold=0.9)
        if not pos:
            return False
        self.click(*pos)
        time.sleep(0.3)
        return True

    def find_click_element_start(self):
        """点击"开始游戏"弹窗按钮；无弹窗返回False。"""
        pos = self.find_template("element-start.png", threshold=0.9)
        if not pos:
            return False
        self.click(*pos)
        time.sleep(0.4)
        return True

    def find_click_element_next(self):
        """结算页：点击"下一关"按钮。
        关键：用只含"下一关"三字的紧致模板 element-next-text.png，而非整块橙色按钮——
        整块按钮与选技能页"0/2确定"按钮形状颜色几乎一样(误匹配0.846)，会导致脚本
        在选技能页误判结算页狂点下一关(选技能只完成第一轮的真正根因)。
        文字模板实测：真结算页0.98~1.0，选技能页确定按钮仅0.389，阈值0.75安全隔离。
        """
        pos = self.find_template("element-next-text.png", threshold=0.75)
        if not pos:
            return False
        # 下一关按钮在结算页底部，客户区y≈898；加坐标下限排除顶部偶发误匹配
        client_y = pos[1] - self.game_window[1] if self.game_window else pos[1]
        if client_y < 700:
            if self.match_log_enabled:
                print(f"[元素] 下一关文字在y={client_y}误匹配，忽略")
            return False
        self.click(*pos)
        time.sleep(0.4)
        return True

    def find_click_activated_skills(self):
        """关闭每局开局都会弹出的「已激活技能」确认页。

        用户实测：点弹窗中间的技能卡不会消失，必须点下方文字/空白区域；
        即使不点，9 秒后也会自动消失。标题模板用于识别该页，底部提示行兜底。
        截图客户区为 542x1010，底部提示行中心约 (271, 925)。
        """
        title = self.find_template("activated-skills-title.png", threshold=0.85)
        tip = None if title else self.find_template("activated-skills-tip.png", threshold=0.8)
        if not title and not tip:
            return False
        if not self.game_window:
            return False
        # click() 期望屏幕坐标，内部会 ScreenToClient 后发后台消息
        self.click(self.game_window[0] + 271, self.game_window[1] + 925, human_like=False)
        time.sleep(0.5)
        print(f"[技能] 已激活技能确认页，点底部文字区关闭 (标题={bool(title)}, 提示行={bool(tip)})")
        return True

    def find_click_expedition_challenge(self):
        """判断能否点击远征挑战按钮"""
        self.click_first_template(
            ["expedition-challenge.png", "expedition-challenge-1.png"]
        )

    def find_expedition_difficulty(self):
        """判断能否发现远征困难按钮"""
        difficulty_icon = ["expedition-difficulty.png"]
        for icon in difficulty_icon:
            difficulty = self.find_template(icon)
            if difficulty:
                return True
        return False

    def find_expedition_normal(self):
        """判断能否发现远征普通按钮"""
        normal_icon = ["expedition-normal.png"]
        for icon in normal_icon:
            normal = self.find_template(icon)
            if normal:
                return True
        return False

    def find_click_expedition_team_hall(self):
        """判断能否点击远征队伍大厅按钮"""
        self.click_template("expedition-team-hall.png")

    def find_click_expedition_fast_join(self):
        """判断能否点击远征快速加入按钮"""
        self.click_template("expedition-fast-join.png")

    def find_expedition_tickets(self):
        """判断能否发现远征门票按钮"""
        tickets_icon = ["expedition-tickets.png"]
        for icon in tickets_icon:
            tickets = self.find_template(icon)
            if tickets:
                return True
        return False

    def click_expedition_fast_join(self):
        """点击远征快速加入按钮"""
        self.find_click_expedition_team_hall()
        time.sleep(1)
        self.find_click_expedition_fast_join()
        time.sleep(1)
        self.expedition_in_team_max_time = (
            time.time() + self.wait_time
        )  # 当前时间戳+wait_time秒 wait_time秒后重新点击

    def find_click_expedition_ready(self):
        """判断能否点击远征准备按钮"""
        ready_icon = ["expedition-ready.png"]
        for icon in ready_icon:
            ready = self.find_template(icon)
            if ready:
                self.click(*ready)
                time.sleep(0.1)
                break

    def find_expedition_personnels(self):
        """判断能否发现远征人员按钮"""
        personnel_icon = ["expedition-personnel.png"]
        for icon in personnel_icon:
            personnels = self.find_all_templates(icon, 0.9)
            return len(personnels)

    def find_expedition_exit(self):
        """判断能否发现远征退出按钮"""
        exit_icon = ["expedition-exit.png"]
        for icon in exit_icon:
            exit = self.find_template(icon)
            if exit:
                return exit
        return None

    def find_leave_button(self):
        """判断能否点击离开按钮"""
        leave_icon = ["leave-button.png"]
        for icon in leave_icon:
            leave = self.find_template(icon)
            if leave:
                return leave
        return None

    def find_click_huanqiu_challenge(self):
        """判断能否点击环球挑战按钮"""
        self.click_template("huanqiu-challenge.png")

    def find_huanqiu_invite(self):
        """判断能否发现环球邀请按钮"""
        invite_icon = ["huanqiu-invite.png"]
        for icon in invite_icon:
            invite = self.find_template(icon)
            if invite:
                return invite
        return None

    def find_click_huanqiu_post_recruitment(self):
        """判断能否点击环球发布招募按钮"""
        self.click_template("huanqiu-post-recruitmen.png")

    def find_click_start_game_button(self):
        """判断能否点击开始游戏按钮"""
        self.click_template("start-game-button.png")

    def find_expedition_vice_captain(self):
        """判断能否远征副队长按钮"""
        vice_captain_icon = ["expedition-vice-captain.png"]
        for icon in vice_captain_icon:
            vice_captain = self.find_template(icon)
            if vice_captain:
                return True
        return False

    def find_expedition_vice_captain_tag(self):
        """判断能否发现远征副队长标签按钮"""
        vice_captain_tag_icon = ["expedition-vice-captain-tag.png"]
        for icon in vice_captain_tag_icon:
            vice_captain_tag = self.find_template(icon)
            if vice_captain_tag:
                return True
        return False

    def find_expedition_elite_tag(self):
        """判断能否发现远征精英标签按钮"""
        elite_tag_icon = ["expedition-elite-tag.png"]
        for icon in elite_tag_icon:
            elite_tag = self.find_template(icon)
            if elite_tag:
                return elite_tag
        return None

    def find_click_start_challenge(self):
        """判断能否点击开始挑战按钮"""
        self.click_template("start-challenge.png")

    def find_expedition_health_100s(self):
        """判断能否发现远征健康值100按钮"""
        expedition_health_icon = ["expedition-health-100.png"]
        for icon in expedition_health_icon:
            expedition_healths = self.find_all_templates(icon)
            if len(expedition_healths) > 0:
                return expedition_healths
        return []

    def find_click_expedition_continue(self):
        """判断能否点击远征继续按钮"""
        self.click_template("expedition-continue.png")

    def find_click_think_tank(self):
        """判断能否点击智库按钮"""
        self.click_template("think_tank.png")

    def find_double_abyss(self):
        """判断能否发现双深渊按钮"""
        double_abyss_icon = ["double-abyss.png"]
        for icon in double_abyss_icon:
            double_abyss = self.find_template(icon)
            if double_abyss:
                return double_abyss
        return None
    def expedition_in_team(self, in_expedition):
        """判断是否在远征团队中"""
        if not in_expedition:
            self.click_expedition_fast_join()
        else:
            tickets = self.find_expedition_tickets()
            # 穷B模式
            if tickets and self.rich_mode == 1:
                self.click_expedition_fast_join()
            else:
                if self.rich_mode == 0:
                    vice_captain = self.find_expedition_vice_captain()
                    base_button = self.find_base()
                    if not vice_captain and not base_button:
                        self.find_click_start_game_button()
                self.find_click_expedition_ready()
                self.find_click_sure()

    def _handle_esc(self):
        print("检测到ESC键，正在停止脚本...")
        self.running = False
        self.step_event.set()

    def _handle_f8(self):
        # F8: 临时显示游戏窗口8秒（供查看情况），之后自动藏回屏幕外
        print("F8: 临时显示游戏窗口8秒，之后自动藏回")
        self._peek_until = time.time() + 8
        hwnd = self._get_hwnd()
        if not hwnd:
            return
        try:
            if win32gui.IsIconic(hwnd):
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            vx = win32api.GetSystemMetrics(76)
            vy = win32api.GetSystemMetrics(77)
            vw = win32api.GetSystemMetrics(78)
            cur = win32gui.GetWindowRect(hwnd)
            w = cur[2] - cur[0]
            h = cur[3] - cur[1]
            new_x = vx + vw - w - 30
            new_y = vy + 30
            if new_x < vx:
                new_x = vx
            win32gui.MoveWindow(hwnd, new_x, new_y, w, h, True)
        except Exception as e:
            print(f"F8显示窗口失败: {e}")

    def _poll_hotkeys(self):
        """GetAsyncKeyState 边沿检测热键/鼠标，不安装全局钩子。"""
        esc = bool(win32api.GetAsyncKeyState(win32con.VK_ESCAPE) & 0x8000)
        f8 = bool(win32api.GetAsyncKeyState(win32con.VK_F8) & 0x8000)
        if esc and not self._hotkey_esc_down:
            self._handle_esc()
        if f8 and not self._hotkey_f8_down:
            self._handle_f8()
        self._hotkey_esc_down = esc
        self._hotkey_f8_down = f8

        left = bool(win32api.GetAsyncKeyState(win32con.VK_LBUTTON) & 0x8000)
        if left and not self._mouse_left_down:
            try:
                self._last_click_pos = win32gui.GetCursorPos()
                self._last_click_time = time.time()
            except Exception:
                pass
        self._mouse_left_down = left

    def _hotkey_poll_loop(self):
        """热键轮询线程：替代 pynput，避免微信聊天输入被清空。"""
        while self.running:
            try:
                self._poll_hotkeys()
            except Exception:
                pass
            time.sleep(0.05)

    def setup_hotkey(self):
        """启动热键轮询（非钩子）"""
        print("已设置快捷键: ESC键 - 停止脚本, F8键 - 临时显示游戏窗口8秒")
        print("提示: 热键改为系统轮询，不再使用全局键盘钩子（减轻对微信输入的干扰）")
        self._hotkey_esc_down = False
        self._hotkey_f8_down = False
        self._mouse_left_down = False
        self._hotkey_thread = threading.Thread(target=self._hotkey_poll_loop, daemon=True)
        self._hotkey_thread.start()

    def main_loop(self):
        """主循环"""
        self.setup_hotkey()

        # 后台模式：启动压制线程（窗口移出屏幕外隐藏+防激活+焦点监测）
        if self.background_mode:
            self._suppress_thread = threading.Thread(target=self._suppress_loop, daemon=True)
            self._suppress_thread.start()

        print("开始自动刷图脚本...")
        print("提示: 按下ESC键可以随时停止脚本")

        # timestamp = time.time()
        while self.running:
            # 单步调试：每轮阻塞等待 GUI 点"下一步"，避免自动连跑刷屏。
            # 只有放行后才执行本轮逻辑；停止时 stop_bot 会 set 事件唤醒线程退出。
            if self.debug_mode:
                self.step_count += 1
                print(f"\n[单步] 第 {self.step_count} 步就绪 — 点击 GUI「下一步」执行本轮")
                self.step_event.wait()
                self.step_event.clear()
                if not self.running:
                    break

            # 检查是否达到刷图次数
            if self.max_battle_count > 0 and self.battle_count >= self.max_battle_count:
                print(f"已完成 {self.battle_count} 次刷图，脚本停止")
                self.running = False
                break

            # 确保游戏窗口被找到
            if not self.game_window and not self.find_game_window():
                time.sleep(5)
                continue

            # ==================== 元素试炼模式 (mode=5) ====================
            # 全流程自管理，放在通用点击之前：结算页背景是战斗画面，若走通用
            # find_click_return() 会误点到结算页"返回"而退出。故本模式独立处理
            # 导航/选技能/结算，continue 跳过下方所有通用逻辑。
            if self.mode == 5:
                # 0) 每局开局都会弹「已激活技能」确认页：点下方文字区关闭。
                #    点"下一关"后必然出现；不识别就会一直等，直到过渡超时误走首页导航。
                if self.find_click_activated_skills():
                    if self.element_after_next:
                        self.element_after_next = False  # 已确认进入新一局
                    self.element_pending_since = time.time()
                    continue
                # 1) 结算页最优先：只有约10秒窗口，且背景战斗画面有选技能残留模板
                #    (实测0.70)，若先判选技能会在结算页误点、耗尽窗口。故先找"下一关"。
                if self.find_click_element_next():
                    self.element_count += 1
                    self.element_pending_battle = True
                    self.element_after_next = True
                    self.element_pending_since = time.time()
                    self.battle_count += 1
                    if self.on_battle_count_changed:
                        self.on_battle_count_changed(self.battle_count)
                    print(f"[元素] 结算页点下一关，第 {self.element_count} 轮完成，进入下一局过渡")
                    continue
                # 2) 选技能界面（战斗中最常见，必须高频响应）
                if self.find_click_skill_element():
                    if self.element_after_next:
                        self.element_after_next = False  # 已进入新一局的战斗
                    self.element_pending_since = time.time()
                    continue

                # 3) 待战斗状态：选技能/战斗/倒计时/结算期间一律不导航。
                if self.element_pending_battle:
                    # 点"挑战"后会弹"开始游戏"确认框（本分支会先于第4步执行，
                    # 若不在这里处理，下面的 continue 会让第4步永远轮不到 -> 卡死）。
                    if self.find_click_element_start():
                        print("[元素] 待战斗中点开始游戏，进入战斗")
                        self.element_after_next = False  # 已进入新一局
                        continue
                    # 已回到核心试炼详情页 -> 本轮结束，清状态继续走下面步骤5点挑战
                    # 用低阈值：次数用完时按钮变灰也能识别，交给步骤5判定停止
                    if self.find_element_challenge_greyed():
                        self.element_pending_battle = False
                        self.element_after_next = False
                        self.element_scroll_count = 0
                    elif self.element_after_next:
                        # 结算页点"下一关"后：游戏直接进入下一局，不经过详情页/
                        # 开始弹窗。此时任何页面都可能正在加载，绝不能掉进导航。
                        # 只在此等待(步骤2的选技能/步骤1的结算会在命中时接手)。
                        if time.time() - self.element_pending_since > 180:
                            print("[元素] 下一关过渡超时(180s)，重置状态重新导航")
                            self.element_pending_battle = False
                            self.element_after_next = False
                        continue
                    else:
                        # 仍在战斗/加载中，超时兜底防止卡死
                        if time.time() - self.element_pending_since > 240:
                            print("[元素] 待战斗超时，重置状态")
                            self.element_pending_battle = False
                        continue

                # 4) "开始游戏"弹窗 -> 点它进入战斗，进入待战斗状态
                if self.find_click_element_start():
                    self.element_pending_battle = True
                    self.element_pending_since = time.time()
                    continue

                # 5) 核心试炼详情页 -> 点"挑战"
                # 入口用低阈值(0.78)判断"在详情页"（橙色按钮1.0、灰色按钮0.8x都能命中，
                # 大厅卡片0.65不会命中），再区分按钮是否可点：
                #   0.9命中=橙色可点 -> 点挑战；0.9未命中但0.78命中=灰色 -> 次数用完。
                if self.find_element_challenge_greyed():
                    if self.find_click_element_challenge():
                        print(f"[元素] 第 {self.element_count+1} 轮挑战开始")
                        # 进入待战斗：下一步会出现"开始游戏"弹窗
                        self.element_pending_battle = True
                        self.element_pending_since = time.time()
                        self.element_no_chance_streak = 0
                    else:
                        # 挑战按钮变灰 = 次数用完。连续3次确认防页面切换瞬间误判。
                        self.element_no_chance_streak += 1
                        if self.element_no_chance_streak >= 3:
                            if not self.element_no_chance_logged:
                                print(f"[元素] 剩余次数已用完，共完成 {self.element_count} 轮，脚本停止")
                                self.element_no_chance_logged = True
                            self.running = False
                            break
                        time.sleep(0.5)
                    continue

                # 6) 元素试炼塔页 -> 点中央核心塔
                if self.find_element_tower_page():
                    self.find_click_element_core_tower()
                    continue

                # 7) 历练大厅：找元素试炼标题 -> 点其"挑战"入口（找到即重置下滑计数）
                if self.find_element_title():
                    self.element_scroll_count = 0
                    self.find_click_element_entry()
                    continue

                # 8) 下滑阶段：持续下滑露出底部的元素试炼卡片。
                #    不依赖易滚出屏幕的锚点，只用计数器；上限到了仍未找到就重新导航。
                if self.element_scroll_count > 0 and self.element_scroll_count < 6:
                    self.scroll_lobby_down()
                    self.element_scroll_count += 1
                    time.sleep(0.3)
                    continue

                # 9) 首页/基地 -> 逐层导航进历练大厅，并把下滑计数置1（进入下滑阶段）
                #    点"基地"后必须等页面切换完成再找"历练大厅"，否则第二次点击扑空。
                if self.find_base():
                    self.find_click_base()
                    time.sleep(1.0)
                self.find_click_experience()
                self.element_scroll_count = 1
                print("[元素] 导航：进入历练大厅，准备下滑找元素试炼")
                time.sleep(0.5)
                continue

            # 点击领取
            self.find_click_receive()

            # 关闭按钮
            self.find_click_home_close()

            # 检查是否需要重新连接
            self.find_click_reconnection()

            # 是否确定
            self.find_click_sure()

            # 是不是通关了
            self.find_click_return()

            # 是不是在战斗中
            while True and self.running:

                # 深渊模式：强化弹窗的战斗背景会让 find_battling() 误判为"战斗中"。
                # 但内层循环只会点选技能/继续/重连/返回，都不处理强化弹窗的"确定"，
                # 导致在这个 while 里空转卡死。所以每轮先检查强化弹窗：出现就点确定并
                # 跳出内层循环，交给外层 mode=4 分支接管（它会继续选技能/战斗流程）。
                if self.mode == 4 and self.find_click_abyss_confirm():
                    print("[深渊] 战斗循环中检测到强化弹窗，点确定并跳出")
                    break

                # 元素试炼：结算页背景是战斗画面，会被find_battling()误判为战斗中。
                # 每轮先检测结算页"下一关"按钮，点到就跳出内层循环，标记本轮完成。
                battling = self.find_battling()
                if not battling:
                    break
                # print("正在战斗中")
                # 点击技能
                self.find_click_skill()
                # 点击继续战斗
                self.find_click_auto_close()
                # time.sleep(1)
                # 点击重新连接
                self.find_click_reconnection()
                # 关闭窗口
                self.find_click_close()
                # 点击返回
                self.find_click_return()

                if (
                    self.battle_time > 0
                    and time.time() - self.current_battle_time > self.battle_time
                ) or (
                    self.mode == 1
                    and self.quick_exit
                ):
                    print(f"战斗时间超过{self.battle_time}秒,退出")
                    # stop_button = self.find_stop()
                    # if stop_button:
                    #     self.click(*stop_button)
                    self.force_click_stop()
                    self.find_click_exit()
                # 战斗时间日志节流：最多10秒打印一次，避免刷屏
                now = time.time()
                if now - getattr(self, "_last_battle_time_log", 0) >= 10:
                    self._last_battle_time_log = now
                    print(
                        "战斗时间:",
                        round(time.time() - self.current_battle_time, 1),
                    )

            # 深渊挑战模式 (mode=4)
            # 从首页自动导航进入深渊页，循环挑战直到次数用完。
            # 绝不自动点"+"加次数（避免误触收费），次数由用户手动加。
            if self.mode == 4:
                # 待战斗状态：点挑战后，依次经历 倒计时(约10s,自动过) -> 强化弹窗 -> 选技能 -> 战斗
                # 期间一律不导航（弹窗/选技能会遮住深渊页锚点，误判为"不在深渊页"而乱点基地）。
                if self.abyss_pending_battle:
                    # 1) 强化弹窗出现 -> 点确定进入选技能/战斗
                    if self.find_click_abyss_confirm():
                        continue
                    # 2) 已回到深渊页 -> 说明这一轮打完了（次数用完时挑战按钮会变灰），
                    #    清除待战斗状态，让下面正常判断能否再挑战一次
                    if self.find_abyss_page():
                        self.abyss_pending_battle = False
                        # 不 continue，继续走下面的"在深渊页"逻辑
                    else:
                        # 3) 倒计时/选技能/战斗/结算中 -> 交给战斗循环处理，这里只等待
                        #    超时兜底：60秒还没回深渊页，强制清除避免卡死
                        if time.time() - self.abyss_pending_since > 60:
                            print("[深渊] 待战斗超时，重置状态")
                            self.abyss_pending_battle = False
                        continue

                # 强化弹窗兜底：若刚进循环就遇到弹窗（非待战斗状态），点确定
                if self.find_click_abyss_confirm():
                    continue

                in_abyss = self.find_abyss_page()
                if in_abyss:
                    # 已在深渊挑战页：点"挑战"开始一次
                    if self.find_click_abyss_challenge():
                        self.abyss_count += 1
                        self.abyss_no_chance_logged = False
                        print(f"[深渊] 第 {self.abyss_count} 次挑战开始")
                        # 进入待战斗状态：倒计时->强化弹窗->选技能->战斗->回深渊页
                        self.abyss_pending_battle = True
                        self.abyss_pending_since = time.time()
                    else:
                        # 挑战按钮变灰/消失 = 次数用完
                        if not self.abyss_no_chance_logged:
                            print(f"[深渊] 剩余次数已用完，共完成 {self.abyss_count} 次挑战，脚本停止")
                            self.abyss_no_chance_logged = True
                        self.running = False
                        break
                else:
                    # 不在深渊页：按导航路径逐层进入
                    # 1) 历练大厅 -> 深渊挑战页
                    if self.find_abyss_title():
                        self.find_click_abyss_entry()
                        time.sleep(0.8)
                    else:
                        # 2) 首页/基地 -> 历练大厅 -> (下一轮再找深渊标题)
                        self.find_click_base()
                        self.find_click_experience()
                        time.sleep(0.5)
                # 交给下方的通用"点击继续"和战斗循环处理
                continue

            # 是否刷环球
            if self.mode == 0:
                # 穷B消费模式
                if self.rich_mode == 1:
                    # 先找是不是在招募中
                    self.find_click_recruitment()
                    # 是否在环球队伍 等6秒
                    in_huanqiu_team = self.find_in_huanqiu_team()
                    if in_huanqiu_team:
                        time.sleep(6)
                    # 先确定位置
                    start_button = self.find_click_start_button()
                    if not start_button:
                        # 不打远征
                        self.find_click_dont_battle_return()
                        self.find_click_continue()
                        continue
                    # 检查当前页面是否在环球页面
                    self.find_click_im()
                # 土豪消费模式
                if self.rich_mode == 0:
                    in_huanqiu_team = self.find_in_huanqiu_team()
                    if not in_huanqiu_team:
                        # 打环球
                        self.find_click_base()
                        self.find_click_experience()
                        self.find_click_huanqiu_challenge()
                    else:
                        invite_button = self.find_huanqiu_invite()
                        if invite_button:
                            self.click(*invite_button)
                            time.sleep(0.2)
                            self.find_click_huanqiu_post_recruitment()
                            self.find_click_home_close()
                            time.sleep(1)
                        else:
                            self.find_click_start_game_button()
                            self.find_click_start_challenge()
            # 是否刷卡关
            if self.mode == 1:
                # 先确定位置
                start_button = self.find_click_start_button()
                if not start_button:
                    # 不打远征
                    self.find_click_dont_battle_return()
                    self.find_click_continue()
                    continue
                # 检查当前页面是否在卡关页面
                self.find_click_card()
            if self.mode in [2, 3]:

                # 有可能进入环球队伍
                in_huanqiu_team = self.find_in_huanqiu_team()
                if in_huanqiu_team:
                    self.find_click_dont_battle_return()

                # 先找是不是在远征队伍中
                in_expedition_team = self.find_expedition_team()
                if not in_expedition_team:
                    # 打远征
                    self.find_click_base()
                    self.find_click_experience()
                    # 页面上划200px露出远征挑战按钮
                    self.swipe_up(200)
                    self.find_click_expedition_challenge()
                else:
                    expedition_exit_button = self.find_expedition_exit()
                    if expedition_exit_button:
                        # 是否秒退
                        if self.quick_exit:

                            # stop_button = self.find_stop()
                            # if stop_button:
                            #     self.click(*stop_button)
                            self.force_click_stop()
                            self.find_click_exit()
                            self.click(*expedition_exit_button)
                            self.find_click_sure()
                            continue

                        self.find_click_expedition_continue()
                        self.find_click_close()
                        # 一个人就退出
                        vice_captain_tag = self.find_expedition_vice_captain_tag()
                        # personnel_count = 1")
                        if vice_captain_tag:
                            # print("发现人走光")
                            self.click(*expedition_exit_button)
                            self.find_click_sure()
                        else:
                            elite_tag = self.find_expedition_elite_tag()
                            if elite_tag:
                                # y轴向下100个像素
                                elite_tag = (elite_tag[0], elite_tag[1] + 100)
                                self.click(*elite_tag)
                                time.sleep(0.1)
                                self.find_click_start_challenge()
                            expedition_healths = self.find_expedition_health_100s()
                            if len(expedition_healths) == 1:
                                expedition_health = expedition_healths[0]
                                # y轴向上100个像素
                                expedition_health = (
                                    expedition_health[0],
                                    expedition_health[1] - 100,
                                )
                                self.click(*expedition_health)
                                time.sleep(0.1)
                                self.find_click_start_challenge()
                if self.mode == 2:
                    in_normal = self.find_expedition_normal()
                    if not in_normal:
                        in_normal = not self.find_expedition_difficulty()
                    self.expedition_in_team(in_normal)
                if self.mode == 3:
                    in_difficulty = self.find_expedition_difficulty()
                    self.expedition_in_team(in_difficulty)
                if time.time() > self.expedition_in_team_max_time:
                    print("等待时间超过最大时间,重新点击")
                    self.click_expedition_fast_join()
                else:
                    remain_time = self.expedition_in_team_max_time - time.time()
                    remain_time = int(remain_time)
                    print(f"等待时间剩余{remain_time}秒")
            # 点击继续
            self.find_click_continue()
            # 每100秒随机点个位置
            # if time.time() - timestamp > 100:
            # 随机点击
            # self.click(random.randint(int(self.game_window[2]), int(self.game_window[0])), random.randint(int(self.game_window[1]), int(self.game_window[3])))
            # self.click(500, 100)
            # timestamp = time.time()


class OutputRedirector:
    """将print输出重定向到Tkinter文本控件，同时写入日志文件便于外部监控"""

    def __init__(self, text_widget, max_lines=100, log_file=None):
        self.text_widget = text_widget
        self.max_lines = max_lines
        self._queue = []
        self._lock = threading.Lock()
        self._log_file = log_file

    def write(self, text):
        # 写入日志文件（便于外部进程监控运行状态）
        if self._log_file:
            try:
                with open(self._log_file, "a", encoding="utf-8") as f:
                    f.write(f"[{time.strftime('%H:%M:%S')}] {text}")
            except Exception:
                pass
        if self.text_widget and self.text_widget.winfo_exists():
            with self._lock:
                self._queue.append(text)
            # 使用after确保在主线程更新GUI
            self.text_widget.after(0, self._flush)

    def _flush(self):
        with self._lock:
            lines = self._queue[:]
            self._queue.clear()

        for text in lines:
            self.text_widget.insert(tk.END, text)
            self.text_widget.see(tk.END)

        # 限制最大行数
        current_lines = int(self.text_widget.index(tk.END).split('.')[0]) - 1
        if current_lines > self.max_lines:
            self.text_widget.delete('1.0', f'{current_lines - self.max_lines}.0')

    def flush(self):
        pass

    def isatty(self):
        return False


class GameBotGUI:
    CONFIG_FILE = "config.json"

    def __init__(self, root):
        self.root = root
        self.root.title("游戏机器人操作界面")
        self.root.geometry("600x800")
        self.root.resizable(True, True)

        self.bot = None
        self.is_running = False

        # 创建界面组件
        self.create_widgets()

        # 加载保存的配置
        self.load_config()

        # 重定向print输出到控制台
        self._redirect_output()

    def get_skill_template_by_name(self, name):
        """根据技能名称获取模板文件名列表"""
        for skill in SKILL_LIST:
            if skill["name"] == name:
                return skill["template"]
        return None

    def load_config(self):
        """加载保存的配置"""
        try:
            if os.path.exists(self.CONFIG_FILE):
                with open(self.CONFIG_FILE, "r", encoding="utf-8") as f:
                    config = json.load(f)
                    # 加载游戏窗口标题
                    game_title = config.get("game_title", "向僵尸开炮")
                    self.game_title_var.set(game_title)
                    # 加载模式
                    mode = config.get("mode", "环球")
                    if mode in ["环球", "主线", "深渊挑战", "元素试炼", "普通远征", "超级远征"]:
                        self.mode_var.set(mode)
                    # 加载消费模式
                    rich_mode = config.get("rich_mode", 0)
                    self.rich_mode_var.set(rich_mode)
                    # 加载秒退模式
                    quick_exit = config.get("quick_exit", False)
                    self.quick_exit_var.set(quick_exit)
                    # 加载后台模式
                    background_mode = config.get("background_mode", True)
                    self.background_mode_var.set(background_mode)
                    # 加载匹配日志开关（默认关闭）
                    match_log_enabled = config.get("match_log_enabled", False)
                    self.match_log_var.set(match_log_enabled)
                    # 加载单步调试开关（默认关闭）
                    debug_mode = config.get("debug_mode", False)
                    self.debug_mode_var.set(debug_mode)
                    # 加载战斗次数
                    max_battle_count = config.get("max_battle_count", 0)
                    self.max_battle_count_var.set(max_battle_count)
                    # 加载战斗时间
                    battle_time = config.get("battle_time", 0)
                    self.battle_time_var.set(battle_time)
                    recruit_click_interval = config.get("recruit_click_interval", 0.05)
                    self.recruit_interval_var.set(recruit_click_interval)
                    # 加载优先技能配置
                    priority_skills = config.get("priority_skills", [])
                    for i, skill_name in enumerate(priority_skills):
                        if i < len(self.priority_skill_vars):
                            self.priority_skill_vars[i].set(skill_name)
                    # 触发模式变化更新显示
                    self.on_mode_changed(None)
        except Exception as e:
            print(f"加载配置失败: {e}")

    def save_config(self):
        """保存当前配置"""
        try:
            config = {
                "game_title": self.game_title_var.get(),
                "mode": self.mode_var.get(),
                "rich_mode": self.rich_mode_var.get(),
                "quick_exit": self.quick_exit_var.get(),
                "background_mode": self.background_mode_var.get(),
                "match_log_enabled": self.match_log_var.get(),
                "debug_mode": self.debug_mode_var.get(),
                "max_battle_count": self.max_battle_count_var.get(),
                "battle_time": self.battle_time_var.get(),
                "recruit_click_interval": self.recruit_interval_var.get(),
                "priority_skills": [var.get() for var in self.priority_skill_vars]
            }
            with open(self.CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(config, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"保存配置失败: {e}")

    def create_widgets(self):
        for i in range(17):
            self.root.grid_rowconfigure(i, minsize=40)

        # 游戏标题
        ttk.Label(self.root, text="游戏窗口标题:").grid(
            row=0, column=0, padx=10, pady=5, sticky=tk.W
        )
        self.game_title_var = tk.StringVar(value="向僵尸开炮")
        self.game_title_entry = ttk.Entry(
            self.root, textvariable=self.game_title_var, width=30
        )
        self.game_title_entry.grid(row=0, column=1, padx=10, pady=5, sticky=tk.W)

        # 模式选择
        ttk.Label(self.root, text="模式:").grid(
            row=1, column=0, padx=10, pady=5, sticky=tk.W
        )
        self.mode_var = tk.StringVar(value="环球")
        self.mode_combo = ttk.Combobox(
            self.root,
            textvariable=self.mode_var,
            values=["环球", "主线", "深渊挑战", "元素试炼", "普通远征", "超级远征"],
            width=15,
            state="readonly",
        )
        self.mode_combo.grid(row=1, column=1, padx=10, pady=5, sticky=tk.W)
        self.mode_combo.bind("<<ComboboxSelected>>", self.on_mode_changed)

        # 消费模式选择（仅环球、普通远征、超级远征显示）
        self.rich_mode_label = ttk.Label(self.root, text="消费模式:")
        self.rich_mode_label.grid(row=2, column=0, padx=10, pady=5, sticky=tk.W)
        self.rich_mode_frame = ttk.Frame(self.root)
        self.rich_mode_var = tk.IntVar(value=0)
        ttk.Radiobutton(
            self.rich_mode_frame, text="我是土豪", variable=self.rich_mode_var, value=0
        ).pack(side=tk.LEFT, padx=5)
        ttk.Radiobutton(
            self.rich_mode_frame, text="我是穷B", variable=self.rich_mode_var, value=1
        ).pack(side=tk.LEFT, padx=5)
        self.rich_mode_frame.grid(row=2, column=1, padx=10, pady=5, sticky=tk.W)

        # 秒退选项（仅普通远征、超级远征、主线显示）
        self.quick_exit_label = ttk.Label(self.root, text="秒退模式:")
        self.quick_exit_label.grid(row=3, column=0, padx=10, pady=5, sticky=tk.W)
        self.quick_exit_var = tk.BooleanVar(value=False)
        self.quick_exit_check = ttk.Checkbutton(
            self.root, text="开启秒退", variable=self.quick_exit_var
        )
        self.quick_exit_check.grid(row=3, column=1, padx=10, pady=5, sticky=tk.W)

        # 运行选项（后台模式 + 诊断日志），放在同一格纵向堆叠，不占用动态显隐的行
        run_opts = ttk.Frame(self.root)
        run_opts.grid(row=3, column=2, padx=10, pady=2, sticky=tk.W)

        # 后台模式（不占用鼠标键盘，游戏窗口不能最小化）
        self.background_mode_var = tk.BooleanVar(value=True)
        self.background_mode_check = ttk.Checkbutton(
            run_opts,
            text="后台运行（不占用鼠标键盘）",
            variable=self.background_mode_var,
        )
        self.background_mode_check.grid(row=0, column=0, sticky=tk.W)

        # 匹配日志（排查用，默认关闭）：开启后记录模板匹配分数到 bot.log
        self.match_log_var = tk.BooleanVar(value=False)
        self.match_log_check = ttk.Checkbutton(
            run_opts,
            text="记录匹配日志（排查用，会写入 bot.log）",
            variable=self.match_log_var,
        )
        self.match_log_check.grid(row=1, column=0, sticky=tk.W)

        # 单步调试（排查用，默认关闭）：开启后主循环每轮需点"下一步"才执行，避免刷屏
        self.debug_mode_var = tk.BooleanVar(value=False)
        self.debug_mode_check = ttk.Checkbutton(
            run_opts,
            text="单步调试（点“下一步”才走一步，避免刷屏）",
            variable=self.debug_mode_var,
        )
        self.debug_mode_check.grid(row=2, column=0, sticky=tk.W)

        # 默认模式是环球，所以默认显示
        self.on_mode_changed(None)

        # 战斗次数
        ttk.Label(self.root, text="战斗次数:").grid(
            row=4, column=0, padx=10, pady=5, sticky=tk.W
        )
        self.max_battle_count_var = tk.IntVar(value=0)
        self.max_battle_count_spinbox = ttk.Spinbox(
            self.root, from_=0, to=999, textvariable=self.max_battle_count_var, width=10
        )
        self.max_battle_count_spinbox.grid(
            row=4, column=1, padx=10, pady=5, sticky=tk.W
        )
        ttk.Label(self.root, text="(0表示无限循环)").grid(
            row=4, column=2, padx=5, pady=5, sticky=tk.W
        )

        # 战斗时间
        ttk.Label(self.root, text="战斗时间(秒):").grid(
            row=5, column=0, padx=10, pady=5, sticky=tk.W
        )
        self.battle_time_var = tk.IntVar(value=0)
        self.battle_time_spinbox = ttk.Spinbox(
            self.root, from_=0, to=999, textvariable=self.battle_time_var, width=10
        )
        self.battle_time_spinbox.grid(row=5, column=1, padx=10, pady=5, sticky=tk.W)
        ttk.Label(self.root, text="(0表示无限制)").grid(
            row=5, column=2, padx=5, pady=5, sticky=tk.W
        )

        ttk.Label(self.root, text="抢队点间隔(秒):").grid(
            row=6, column=0, padx=10, pady=5, sticky=tk.W
        )
        self.recruit_interval_var = tk.DoubleVar(value=0.05)
        self.recruit_interval_spinbox = ttk.Spinbox(
            self.root,
            from_=0.02,
            to=0.2,
            increment=0.01,
            textvariable=self.recruit_interval_var,
            width=10,
        )
        self.recruit_interval_spinbox.grid(
            row=6, column=1, padx=10, pady=5, sticky=tk.W
        )
        ttk.Label(self.root, text="(新队抢中率对比用)").grid(
            row=6, column=2, padx=5, pady=5, sticky=tk.W
        )

        # 优先技能选项（5个）
        ttk.Label(self.root, text="优先技能(从上到下):").grid(
            row=7, column=0, padx=10, pady=5, sticky=tk.W
        )

        # 获取技能名称列表
        skill_names = [skill["name"] for skill in SKILL_LIST]

        # 5个优先技能下拉框
        self.priority_skill_vars = []
        self.priority_skill_combos = []
        for i in range(5):
            var = tk.StringVar(value="")
            self.priority_skill_vars.append(var)
            combo = ttk.Combobox(
                self.root,
                textvariable=var,
                values=[""] + skill_names,
                width=15,
                state="readonly",
            )
            combo.grid(row=7 + i, column=1, padx=10, pady=3, sticky=tk.W)
            combo.bind("<<ComboboxSelected>>", self.on_skill_selected)
            self.priority_skill_combos.append(combo)
            ttk.Label(self.root, text=f"优先级{i+1}").grid(
                row=7 + i, column=2, padx=5, pady=3, sticky=tk.W
            )

        # 按钮框架
        button_frame = ttk.Frame(self.root)
        button_frame.grid(row=12, column=0, columnspan=3, padx=10, pady=20)

        # 开始按钮
        self.start_btn = ttk.Button(
            button_frame, text="开始", command=self.start_bot, width=15
        )
        self.start_btn.pack(side=tk.LEFT, padx=10)

        # 停止按钮
        self.stop_btn = ttk.Button(
            button_frame,
            text="停止",
            command=self.stop_bot,
            width=15,
            state=tk.DISABLED,
        )
        self.stop_btn.pack(side=tk.LEFT, padx=10)

        # 单步按钮（仅单步调试模式可用）：点一下放行主循环一轮
        self.step_btn = ttk.Button(
            button_frame,
            text="下一步 ▶",
            command=self.step_once,
            width=15,
            state=tk.DISABLED,
        )
        self.step_btn.pack(side=tk.LEFT, padx=10)

        # 调整窗口大小按钮
        self.resize_btn = ttk.Button(
            button_frame, text="调整窗口大小", command=self.resize_window, width=15
        )
        self.resize_btn.pack(side=tk.LEFT, padx=10)

        # 退出按钮
        self.quit_btn = ttk.Button(
            button_frame, text="退出", command=self.quit_app, width=15
        )
        self.quit_btn.pack(side=tk.LEFT, padx=10)

        # 状态标签
        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(self.root, textvariable=self.status_var, foreground="green").grid(
            row=13, column=0, columnspan=3, padx=10, pady=10, sticky=tk.W
        )

        # 战斗次数标签
        self.battle_count_var = tk.StringVar(value="战斗次数: 0")
        ttk.Label(
            self.root, textvariable=self.battle_count_var, foreground="blue"
        ).grid(row=14, column=0, columnspan=3, padx=10, pady=5, sticky=tk.W)

        # 提示标签
        ttk.Label(self.root, text="提示: 按ESC键暂停脚本", foreground="blue").grid(
            row=15, column=0, columnspan=3, padx=10, pady=5, sticky=tk.W
        )

        # 控制台输出区域
        console_frame = ttk.LabelFrame(self.root, text="控制台输出")
        console_frame.grid(row=16, column=0, columnspan=3, padx=10, pady=10, sticky=tk.W + tk.E + tk.N + tk.S)
        self.root.grid_rowconfigure(16, minsize=150)

        self.console_text = scrolledtext.ScrolledText(
            console_frame, width=70, height=8, state=tk.NORMAL, wrap=tk.WORD
        )
        self.console_text.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

    def on_skill_selected(self, event):
        """技能选择事件，防止重复选择"""
        # 获取所有已选择的技能
        selected_skills = [var.get() for var in self.priority_skill_vars if var.get()]

        # 获取所有技能名称
        all_skills = [skill["name"] for skill in SKILL_LIST]

        # 更新每个下拉框的可选项
        for i, combo in enumerate(self.priority_skill_combos):
            current_value = self.priority_skill_vars[i].get()
            # 可选项：空 + 未被其他下拉框选中的技能
            available = [""] + [
                s for s in all_skills if s not in selected_skills or s == current_value
            ]
            combo["values"] = available

    def _update_battle_count(self, count):
        """更新战斗次数显示（线程安全）"""
        self.root.after(0, lambda: self.battle_count_var.set(f"战斗次数: {count}"))

    def _redirect_output(self):
        """重定向print输出到控制台文本框，并写入bot.log"""
        self._stdout_redirector = OutputRedirector(
            self.console_text, max_lines=100, log_file="bot.log"
        )
        sys.stdout = self._stdout_redirector

    def on_mode_changed(self, event):
        """模式选择事件，控制土豪/穷B单选框和秒退选项显示"""
        mode = self.mode_var.get()
        # 仅环球、普通远征、超级远征显示土豪/穷B选项
        if mode in ["环球", "普通远征", "超级远征"]:
            self.rich_mode_label.grid(row=2, column=0, padx=10, pady=5, sticky=tk.W)
            self.rich_mode_frame.grid(row=2, column=1, padx=10, pady=5, sticky=tk.W)
        else:
            self.rich_mode_label.grid_remove()
            self.rich_mode_frame.grid_remove()

        # 仅普通远征、超级远征、主线显示秒退选项
        if mode in ["普通远征", "超级远征", "主线"]:
            self.quick_exit_label.grid(row=3, column=0, padx=10, pady=5, sticky=tk.W)
            self.quick_exit_check.grid(row=3, column=1, padx=10, pady=5, sticky=tk.W)
        else:
            self.quick_exit_label.grid_remove()
            self.quick_exit_check.grid_remove()

    def start_bot(self):
        """开始运行游戏机器人"""
        try:
            # 获取界面参数
            game_title = self.game_title_var.get()
            mode_text = self.mode_var.get()
            mode_map = {"环球": 0, "主线": 1, "普通远征": 2, "超级远征": 3, "深渊挑战": 4, "元素试炼": 5}
            mode = mode_map.get(mode_text, 0)
            max_battle_count = self.max_battle_count_var.get()
            battle_time = self.battle_time_var.get()
            rich_mode = self.rich_mode_var.get()
            quick_exit = self.quick_exit_var.get()
            debug_mode = self.debug_mode_var.get()

            # 获取5个优先技能（将中文名称转换为模板文件名）
            priority_skills = []
            for var in self.priority_skill_vars:
                skill_name = var.get()
                if skill_name:
                    template = self.get_skill_template_by_name(skill_name)
                    if template:
                        priority_skills.append(template)

            # 保存配置
            self.save_config()

            # 验证参数
            if not game_title:
                messagebox.showerror("错误", "请输入游戏窗口标题")
                return

            # 创建GameBot实例
            self.bot = GameBot(
                game_title,
                battle_time,
                max_battle_count,
                mode,
                priority_skills,
                rich_mode,
                60,
                quick_exit,
                self.background_mode_var.get(),
                self.match_log_var.get(),
                debug_mode,
                self.recruit_interval_var.get(),
            )
            self.bot.on_battle_count_changed = self._update_battle_count
            if self.match_log_var.get():
                print("[匹配日志] 已开启：模板匹配分数将记录到 bot.log")
            print(
                f"[抢队] 点间隔 {self.recruit_interval_var.get():.2f}s，"
                f"按新队伍出现统计抢中率"
            )

            # 更新状态
            if debug_mode:
                self.status_var.set("单步调试：点「下一步」开始第 1 步")
                self.step_btn.config(state=tk.NORMAL)
            else:
                self.status_var.set("运行中...")
                self.step_btn.config(state=tk.DISABLED)
            self.battle_count_var.set("战斗次数: 0")
            self.start_btn.config(state=tk.DISABLED)
            self.stop_btn.config(state=tk.NORMAL)

            # 禁用所有参数控件
            self.game_title_entry.config(state=tk.DISABLED)
            self.mode_combo.config(state=tk.DISABLED)
            for child in self.rich_mode_frame.winfo_children():
                child.config(state=tk.DISABLED)
            self.quick_exit_check.config(state=tk.DISABLED)
            self.background_mode_check.config(state=tk.DISABLED)
            self.match_log_check.config(state=tk.DISABLED)
            self.debug_mode_check.config(state=tk.DISABLED)
            self.max_battle_count_spinbox.config(state=tk.DISABLED)
            self.battle_time_spinbox.config(state=tk.DISABLED)
            self.recruit_interval_spinbox.config(state=tk.DISABLED)
            for combo in self.priority_skill_combos:
                combo.config(state=tk.DISABLED)
            self.resize_btn.config(state=tk.DISABLED)

            # 创建并启动线程
            self.bot_thread = threading.Thread(target=self.run_bot, daemon=True)
            self.bot_thread.start()

        except Exception as e:
            messagebox.showerror("错误", f"启动失败: {str(e)}")
            self.status_var.set("就绪")

    def step_once(self):
        """单步调试：放行主循环执行一轮，然后重新阻塞等待下一次点击"""
        bot = self.bot
        if bot and bot.debug_mode and bot.running:
            bot.step_event.set()
            self.status_var.set(
                f"单步调试：已放行第 {bot.step_count} 步 — 等待下一次点击"
            )

    def run_bot(self):
        """运行游戏机器人主循环"""
        try:
            # 运行主循环，直到达到指定次数或被停止
            while self.bot and self.bot.running:
                # 运行一次主循环迭代
                self.bot.main_loop()
                # 短暂休眠，避免CPU占用过高
                time.sleep(0.1)
        except Exception as e:
            print(f"运行出错: {str(e)}")
        finally:
            # 停止运行
            self.root.after(0, self.stop_bot)

    def stop_bot(self):
        """停止游戏机器人"""
        if self.bot:
            self.bot.running = False
            # 单步模式下线程可能阻塞在 step_event.wait()，唤醒它以便线程退出
            self.bot.step_event.set()
            if self.bot._recruit_new:
                self.bot._print_recruit_stats("本轮汇总")
            self.bot = None

        # 更新状态
        self.status_var.set("已停止")
        self.start_btn.config(state=tk.NORMAL)
        self.stop_btn.config(state=tk.DISABLED)
        self.step_btn.config(state=tk.DISABLED)

        # 启用所有参数控件
        self.game_title_entry.config(state=tk.NORMAL)
        self.mode_combo.config(state=tk.NORMAL)
        for child in self.rich_mode_frame.winfo_children():
            child.config(state=tk.NORMAL)
        self.quick_exit_check.config(state=tk.NORMAL)
        self.background_mode_check.config(state=tk.NORMAL)
        self.match_log_check.config(state=tk.NORMAL)
        self.debug_mode_check.config(state=tk.NORMAL)
        self.max_battle_count_spinbox.config(state=tk.NORMAL)
        self.battle_time_spinbox.config(state=tk.NORMAL)
        self.recruit_interval_spinbox.config(state=tk.NORMAL)
        for combo in self.priority_skill_combos:
            combo.config(state=tk.NORMAL)
        self.resize_btn.config(state=tk.NORMAL)

    def resize_window(self):
        """调整游戏窗口大小"""
        try:
            # 创建临时GameBot实例来调整窗口大小
            game_title = self.game_title_var.get()
            temp_bot = GameBot(game_title=game_title)
            if temp_bot.resize_game_window():
                self.status_var.set("窗口大小已调整为542*1010")
            else:
                self.status_var.set("未找到游戏窗口，无法调整大小")
        except Exception as e:
            self.status_var.set(f"调整窗口大小失败: {e}")

    def quit_app(self):
        """退出应用程序"""
        if self.bot:
            self.bot.running = False
            self.bot.step_event.set()  # 唤醒单步阻塞，让线程尽快退出
        # 恢复标准输出
        if hasattr(self, '_stdout_redirector'):
            sys.stdout = sys.__stdout__
        self.root.quit()


if __name__ == "__main__":
    # 创建主窗口
    root = tk.Tk()

    # 设置窗口图标（可选）
    try:
        root.iconbitmap(default=None)
    except:
        pass

    # 创建GUI实例
    app = GameBotGUI(root)

    # 运行主循环
    root.mainloop()

# 使用说明:
# 1. 安装必要的依赖: pip install pyautogui opencv-python pillow pywin32
# 2. 替换脚本中的游戏窗口标题为你的游戏窗口标题
# 3. 在 templates 文件夹中添加游戏界面元素的截图作为模板
# 4. 运行脚本: python game_bot.py
# 5. 脚本会自动查找游戏窗口，开始战斗，收集奖励
#
# 注意事项:
# - 本脚本仅提供基础框架，需要根据具体游戏进行调整
# - 为了提高识别准确率，建议使用游戏窗口的原始分辨率
# - 使用时请确保游戏窗口未被遮挡
# - 可以通过添加更多的模板和状态判断来提高脚本的智能性
# - 游戏过程中尽量不要操作鼠标和键盘，以免干扰脚本运行
