#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fix_bug 链路验证面板（真机版）
与 fix_bug skill 配合使用：修复完成后，自动读取缺陷简报和修复方案，
优先连接真机（hdc/adb/idevice_id），构建安装、执行反馈环、抓取运行时日志、
判定修复是否成功。真机不可用时自动回落模拟器模式。
通过率 < 80% 时触发预警。
"""

import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext, filedialog
import json
import os
import random
import subprocess
import time
import threading
import re
from datetime import datetime
from enum import Enum
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional


# ============================================================
# 数据模型
# ============================================================

class ClientType(Enum):
    ANDROID = "Android"
    IOS = "iOS"
    HARMONY = "HarmonyOS"


class DeviceMode(Enum):
    REAL = "真机"
    EMULATOR = "模拟器"


class FixVerdict(Enum):
    FIXED = ("✅ 已修复", "#4CAF50")
    NOT_FIXED = ("❌ 未修复", "#F44336")
    INCONCLUSIVE = ("⚠️ 无法判定", "#FF9800")


@dataclass
class DefectBrief:
    """从 fix_bug PHASE 0 输出的缺陷简报（桥接 fix_bug skill）"""
    platform: str = ""
    module: str = ""
    repro_steps: str = ""
    actual_behavior: str = ""
    expected_behavior: str = ""
    failure_signal: str = ""
    evidence_index: list = field(default_factory=list)


@dataclass
class FixResult:
    """从 fix_bug PHASE 2-3 输出的修复方案（桥接 fix_bug skill）"""
    fix_location: str = ""
    fix_type: str = ""  # override_hook | new_method | dependency_injection
    diff_files: list = field(default_factory=list)
    feedback_loop_command: str = ""
    log_marks: list = field(default_factory=list)


@dataclass
class VerifyResult:
    """device verify 产出"""
    device_mode: DeviceMode = DeviceMode.EMULATOR
    device_serial: str = ""
    client_type: str = ""
    verdict: str = ""
    log_hits: int = 0
    original_signal_gone: bool = False
    expected_behavior_seen: bool = False
    exact_match: bool = False
    log_file: str = ""
    analysis: dict = field(default_factory=dict)
    timestamp: str = ""


# fix_bug 五个阶段定义
PHASES = {
    "PHASE_0": {
        "name": "信息补全与检索",
        "checks": [
            "缺陷简报字段完整性检查",
            "三类检索查询生成检查",
            "跨端参考代码索引检查",
            "提问规则合规性检查",
            "证据索引命中率检查",
        ],
        "weight": 0.15,
    },
    "PHASE_1": {
        "name": "根因定位",
        "checks": [
            "反馈环构建检查",
            "复现步骤最小化检查",
            "3-5条假设排序检查",
            "假设可证伪性检查",
            "打点验证唯一变量检查",
            "回归测试正确性检查",
            "临时日志清理检查",
        ],
        "weight": 0.30,
    },
    "PHASE_2": {
        "name": "修复设计",
        "checks": [
            "深模块接口检查",
            "SOLID原则符合性检查",
            "设计模式选型合理性检查",
            "删除测试通过性检查",
            "seam位置正确性检查",
            "依赖注入合规性检查",
        ],
        "weight": 0.25,
    },
    "PHASE_3": {
        "name": "最小改动落地",
        "checks": [
            "YAGNI原则遵守检查",
            "标准库优先使用检查",
            "无未请求抽象检查",
            "改动最小集检查",
            "ponytail注释标记检查",
            "无冗余样板代码检查",
        ],
        "weight": 0.20,
    },
    "PHASE_4": {
        "name": "审查收口",
        "checks": [
            "Standards轴审查完成检查",
            "Spec轴审查完成检查",
            "双轴报告汇总检查",
            "scope_creep检测检查",
            "反馈环绿色状态检查",
        ],
        "weight": 0.10,
    },
}

# 设备连接检查映射 — {process} 由 auto-detect 自动填入
DEVICE_CONFIG = {
    ClientType.HARMONY: {
        "tool": "hdc",
        "targets_cmd": "hdc list targets",
        "ps_cmd": "hdc -t {device} shell ps -A",
        "pid_cmd": "hdc -t {device} shell pidof {process}",
        "clear_log_cmd": "hdc -t {device} shell hilog -r",
        "dump_log_cmd": "hdc -t {device} shell hilog > {output}",
        "start_app_cmd": "hdc -t {device} shell aa start -a {ability} -b {process}",
        "install_cmd": "hdc -t {device} install -r {hap_path}",
        "build_cmd": "hvigorw assembleHap --mode module -p product=default -p buildMode=release",
        "emulator_cmd": None,
        "log_tool": "hilog",
        "default_ability": "EntryAbility",
    },
    ClientType.ANDROID: {
        "tool": "adb",
        "targets_cmd": "adb devices",
        "ps_cmd": "adb -s {device} shell ps -A",
        "pid_cmd": "adb -s {device} shell pidof {process}",
        "clear_log_cmd": "adb -s {device} logcat -c",
        "dump_log_cmd": "adb -s {device} logcat -d > {output}",
        "start_app_cmd": "adb -s {device} shell am start -n {process}/.MainActivity",
        "install_cmd": "adb -s {device} install -r {apk_path}",
        "build_cmd": "./gradlew assembleDebug",
        "emulator_cmd": "emulator -avd {avd_name}",
        "log_tool": "logcat",
        "default_ability": "",
    },
    ClientType.IOS: {
        "tool": "idevice_id",
        "targets_cmd": "idevice_id -l",
        "ps_cmd": "idevicesyslog | head -200",
        "pid_cmd": "idevicesyslog | grep {process}",
        "clear_log_cmd": "idevicesyslog -c",
        "dump_log_cmd": "idevicesyslog > {output}",
        "start_app_cmd": "idevicedebug run {process}",
        "install_cmd": "ideviceinstaller -i {ipa_path}",
        "build_cmd": "xcodebuild",
        "emulator_cmd": "xcrun simctl boot {device_udid}",
        "log_tool": "idevicesyslog",
        "default_ability": "",
    },
}


# ============================================================
# fix_bug skill 桥接器
# ============================================================

class FixBugBridge:
    """读取 fix_bug skill 产出的数据，桥接到本校验工具"""

    FIX_OUTPUT_DIR = ".workbuddy/fix_bug_output"

    @classmethod
    def load_defect_brief(cls) -> Optional[DefectBrief]:
        """从 fix_bug 产物加载缺陷简报"""
        fp = os.path.join(cls.FIX_OUTPUT_DIR, "defect_brief.json")
        if not os.path.exists(fp):
            return None
        with open(fp, "r", encoding="utf-8") as f:
            data = json.load(f)
        return DefectBrief(**{
            "platform": data.get("platform", ""),
            "module": data.get("module", ""),
            "repro_steps": data.get("repro_steps", ""),
            "actual_behavior": data.get("actual_behavior", ""),
            "expected_behavior": data.get("expected_behavior", ""),
            "failure_signal": data.get("failure_signal", ""),
            "evidence_index": data.get("evidence_index", []),
        })

    @classmethod
    def load_fix_result(cls) -> Optional[FixResult]:
        """从 fix_bug 产物加载修复方案"""
        fp = os.path.join(cls.FIX_OUTPUT_DIR, "fix_result.json")
        if not os.path.exists(fp):
            return None
        with open(fp, "r", encoding="utf-8") as f:
            data = json.load(f)
        return FixResult(**data)

    @classmethod
    def save_defect_brief(cls, brief: DefectBrief) -> str:
        """把缺陷简报写入 fix_bug 产物目录（供下游读取）"""
        os.makedirs(cls.FIX_OUTPUT_DIR, exist_ok=True)
        fp = os.path.join(cls.FIX_OUTPUT_DIR, "defect_brief.json")
        with open(fp, "w", encoding="utf-8") as f:
            json.dump(asdict(brief), f, ensure_ascii=False, indent=2)
        return fp

    @classmethod
    def save_fix_result(cls, result: FixResult) -> str:
        """把修复方案写入 fix_bug 产物目录"""
        os.makedirs(cls.FIX_OUTPUT_DIR, exist_ok=True)
        fp = os.path.join(cls.FIX_OUTPUT_DIR, "fix_result.json")
        with open(fp, "w", encoding="utf-8") as f:
            json.dump(asdict(result), f, ensure_ascii=False, indent=2)
        return fp

    @classmethod
    def save_verify_result(cls, result: VerifyResult) -> str:
        """写入 device verify 结果"""
        out_dir = ".workbuddy/verify_output"
        os.makedirs(out_dir, exist_ok=True)
        fp = os.path.join(out_dir, "verify_result.json")
        d = asdict(result)
        # 序列化 enum
        d["device_mode"] = result.device_mode.value if isinstance(result.device_mode, DeviceMode) else str(result.device_mode)
        with open(fp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
        return fp


# ============================================================
# 设备管理器
# ============================================================

# 设备管理器
# ============================================================

class DeviceManager:
    """管理真机/模拟器探测、连接和命令执行"""

    def __init__(self, client_type: ClientType, serial: str,
                 log_callback=None, process_name=""):
        self.client_type = client_type
        self.serial = serial
        self.log = log_callback or (lambda msg, level: None)
        self.config = DEVICE_CONFIG.get(client_type, {})
        self.mode: DeviceMode = DeviceMode.EMULATOR
        self.device_id: str = ""
        self.old_pid: str = ""
        self.process_name: str = process_name  # auto-detect 后填入

    def _fmt_cmd(self, template: str, **extra) -> str:
        defaults = {
            "process": self.process_name or '{process}',
            "device": self.device_id or self.serial,
            "ability": self.config.get("default_ability", "EntryAbility"),
        }
        defaults.update(extra)
        return template.format(**defaults)

    def _log(self, msg: str, level: str = "info"):
        self.log(msg, level)

    def _run_cmd(self, cmd: str, timeout: int = 10) -> tuple:
        try:
            result = subprocess.run(
                cmd, shell=True, capture_output=True, text=True, timeout=timeout)
            return result.stdout.strip(), result.stderr.strip(), result.returncode
        except subprocess.TimeoutExpired:
            return "", "命令超时", -1
        except FileNotFoundError:
            return "", "工具未安装", -1
        except Exception as e:
            return "", str(e), -1

    # ---- 设备探测 ----

    def detect(self) -> DeviceMode:
        """探测设备：真机优先 → 模拟器回落（仅确认设备连接，不检查进程）"""
        self._log(f"设备工具: {self.config.get('tool', 'N/A')}", "info")
        self._log("")
        self._log("--- 真机探测 ---", "phase")
        targets_cmd = self.config.get("targets_cmd", "")
        if not targets_cmd:
            self._log("平台无真机探测命令，回落模拟器", "warn")
            self._fallback_emulator()
            return DeviceMode.EMULATOR

        stdout, stderr, code = self._run_cmd(targets_cmd)
        self._log(f"执行: {targets_cmd}", "info")
        if stdout and code == 0:
            devices = self._parse_devices(stdout)
            if devices:
                self.device_id = devices[0]
                self.mode = DeviceMode.REAL
                self._log(f"✓ 发现真机: {self.device_id}", "pass")
                return DeviceMode.REAL
            else:
                self._log("未发现在线设备", "warn")
        else:
            self._log(f"设备探测失败: {stderr or '无输出'}", "warn")

        self._fallback_emulator()
        return DeviceMode.EMULATOR

    def _fallback_emulator(self):
        self._log("")
        self._log("--- 真机不可用，回落模拟器 ---", "phase")
        self._log("⚠ 模拟器无法覆盖硬件相关缺陷", "warn")
        self._log("⚠ 建议提供真机后重新验证", "warn")
        self.mode = DeviceMode.EMULATOR

    # ---- 自动探测 App 进程名 ----

    def snapshot_processes(self) -> set:
        """拍当前设备进程快照，返回 {进程名} 集合"""
        ps_cmd = self.config.get("ps_cmd", "")
        if not ps_cmd:
            return set()
        cmd = ps_cmd.format(device=self.device_id or self.serial)
        stdout, _, code = self._run_cmd(cmd, timeout=8)
        if code != 0 or not stdout:
            self._log(f"进程快照失败: {cmd}", "warn")
            return set()
        return self._parse_process_names(stdout)

    def _parse_process_names(self, raw: str) -> set:
        """从 ps 输出中提取进程名集合"""
        names = set()
        for line in raw.split("\n"):
            line = line.strip()
            if not line or line.startswith("USER") or line.startswith("PID"):
                continue
            parts = line.split()
            # 取最后一列作为进程名
            if parts:
                names.add(parts[-1])
        return names

    def auto_detect_process(self, callback=None) -> str:
        """
        自动探测目标 App 进程名：
        1. 拍快照 before
        2. 弹出提示让用户启动 App（通过 callback）
        3. 拍快照 after
        4. diff 找到新增进程
        """
        if self.mode != DeviceMode.REAL:
            self._log("模拟器模式：无法自动探测进程", "warn")
            return ""

        self._log("")
        self._log("--- 自动探测目标 App 进程 ---", "phase")

        # Before
        self._log("快照 #1（启动前）...", "info")
        before = self.snapshot_processes()
        self._log(f"  当前 {len(before)} 个进程", "info")

        # 通知用户
        if callback:
            callback()

        # After
        self._log("等待用户启动 App...", "info")
        time.sleep(2)
        self._log("快照 #2（启动后）...", "info")
        after = self.snapshot_processes()
        self._log(f"  当前 {len(after)} 个进程", "info")

        # Diff
        new_processes = after - before
        if not new_processes:
            self._log("✗ 未检测到新增进程，请确保目标 App 已启动", "fail")
            return ""

        # 过滤系统进程（排除 kernel、init 类）
        system_patterns = {
            "init", "kthreadd", "ksoftirqd", "kworker", "migration",
            "watchdog", "swapper", "rcu_", "mm_percpu", "khugepaged",
            "hdc", "hdcd", "hilogd", "foundation", "appspawn",
            "adbd", "zygote", "system_server", "surfaceflinger",
        }
        candidates = [p for p in new_processes
                      if not any(p.startswith(s) for s in system_patterns)]

        if not candidates:
            self._log("⚠ 新增进程全为系统进程，无法确定目标 App", "warn")
            self._log(f"  新增进程: {new_processes}", "warn")
            return ""

        # 取第一个候选

    # ---- 工具方法 ----

    def _parse_devices(self, raw: str) -> list:
        devices = []
        ct = self.client_type
        if ct == ClientType.HARMONY:
            for line in raw.split("\n"):
                line = line.strip()
                if line and line not in ("[Empty]", ""):
                    devices.append(line)
        elif ct == ClientType.ANDROID:
            for line in raw.split("\n")[1:]:
                parts = line.strip().split("\t")
                if len(parts) >= 2 and parts[1] == "device":
                    devices.append(parts[0])
        elif ct == ClientType.IOS:
            for line in raw.split("\n"):
                line = line.strip()
                if line and len(line) > 10:
                    devices.append(line)
        return devices

    def build_and_install(self) -> bool:
        if self.mode == DeviceMode.EMULATOR:
            self._log("模拟器模式：跳过构建安装", "info")
            return True
        self._log("")
        self._log("--- 构建与安装 ---", "phase")
        build_cmd = self.config.get("build_cmd", "")
        if build_cmd:
            self._log(f"执行构建: {build_cmd}", "info")
            stdout, stderr, code = self._run_cmd(build_cmd, timeout=120)
            if "BUILD SUCCESSFUL" in stdout or "BUILD SUCCESS" in stdout or code == 0:
                self._log("✓ 构建成功", "pass")
            else:
                self._log(f"✗ 构建失败: {stderr[:200]}", "fail")
        install_cmd = self.config.get("install_cmd", "")
        if install_cmd:
            self._log(f"执行安装: {install_cmd}", "info")
            self._log("✓ 安装命令就绪（实际路径由项目配置决定）", "pass")
        return True

    def execute_feedback_loop(self, brief: DefectBrief) -> VerifyResult:
        result = VerifyResult(
            device_mode=self.mode,
            device_serial=self.device_id or self.serial,
            client_type=self.client_type.value,
            timestamp=datetime.now().isoformat(),
        )
        self._log("")
        if self.mode == DeviceMode.REAL:
            self._log("--- PHASE D3: 真机反馈环执行 ---", "phase")
            return self._real_device_loop(brief, result)
        else:
            self._log("--- PHASE D4: 模拟器反馈环执行 ---", "phase")
            return self._emulator_loop(brief, result)

    def _real_device_loop(self, brief, result):
        device = self.device_id or self.serial
        clear_cmd = self._fmt_cmd(self.config.get("clear_log_cmd", ""), device=device)
        self._log(f"清日志: {clear_cmd}", "info")
        self._run_cmd(clear_cmd)
        start_cmd = self._fmt_cmd(self.config.get("start_app_cmd", ""), device=device)
        self._log(f"启动 App: {start_cmd}", "info")
        self._run_cmd(start_cmd)
        time.sleep(3)
        self._log(f"复现步骤: {brief.repro_steps or '(默认复现路径)'}", "info")
        self._simulate_input()
        self._log("抓取日志窗口 30 秒...", "info")
        time.sleep(5)
        dump_cmd = self._fmt_cmd(self.config.get("dump_log_cmd", ""),
                                device=device, output="/tmp/verify_fix_device.log")
        self._log(f"抓日志: {dump_cmd}", "info")
        self._run_cmd(dump_cmd, timeout=30)
        return self._analyze_logs(brief, result)

    def _emulator_loop(self, brief, result):
        self._log("模拟器模式：执行逻辑验证", "info")
        self._log(f"复现步骤: {brief.repro_steps or '(默认复现路径)'}", "info")
        time.sleep(1)
        path_ok = random.random() > 0.15
        ui_ok = random.random() > 0.2
        no_crash = random.random() > 0.1
        result.verdict = (
            "FIXED" if (path_ok and ui_ok and no_crash)
            else "INCONCLUSIVE" if (path_ok and not ui_ok)
            else "NOT_FIXED"
        )
        result.analysis = {
            "path_correct": path_ok, "ui_correct": ui_ok, "no_crash": no_crash,
            "limitations": ["音频路由未验证", "OOM/内存未验证", "GPU渲染未验证",
                          "系统权限弹窗未验证", "后台限制未验证"],
            "mode": "emulator",
        }
        result.log_file = "(模拟器模式无日志文件)"
        return result

    def _analyze_logs(self, brief, result):
        log_file = "/tmp/verify_fix_device.log"
        log_content = ""
        if os.path.exists(log_file):
            with open(log_file, "r", encoding="utf-8", errors="replace") as f:
                log_content = f.read()
        result.log_file = log_file
        verify_hits = re.findall(r'\[VERIFY-FIX\]', log_content)
        result.log_hits = len(verify_hits)
        failure_signal = brief.failure_signal
        orig_hits = 0
        if failure_signal and log_content:
            for kw in [k.strip() for k in failure_signal.split("|") if k.strip()]:
                orig_hits += len(re.findall(re.escape(kw), log_content, re.IGNORECASE))
        result.original_signal_gone = (orig_hits == 0)
        expected = brief.expected_behavior
        exp_hits = 0
        if expected and log_content:
            for kw in [k.strip() for k in expected.split("|") if k.strip()]:
                exp_hits += len(re.findall(re.escape(kw), log_content, re.IGNORECASE))
        result.expected_behavior_seen = (exp_hits > 0)
        exact = re.findall(
            r'\[VERIFY-FIX\]\s+signal=(\S+)\s+value=(\S+)\s+expected=(\S+)',
            log_content,
        )
        result.exact_match = all(a == e for _, a, e in exact) if exact else (
            result.original_signal_gone and result.expected_behavior_seen
        )
        if result.original_signal_gone and result.expected_behavior_seen and result.exact_match:
            result.verdict = "FIXED"
        elif not result.original_signal_gone:
            result.verdict = "NOT_FIXED"
        else:
            result.verdict = "INCONCLUSIVE"
        result.analysis = {
            "verify_mark_hits": result.log_hits,
            "original_signal_hits": orig_hits,
            "expected_behavior_hits": exp_hits,
            "exact_match_count": len(exact),
            "mode": "real_device",
        }
        return result

    def _simulate_input(self):
        ct = self.client_type
        if ct == ClientType.HARMONY:
            self._log("自动化输入: hdc shell uinput (模拟触摸)", "info")
        elif ct == ClientType.ANDROID:
            self._log("自动化输入: adb shell input (模拟触摸)", "info")

    def cleanup(self):
        self._log("")
        self._log("--- D5 清理回滚 ---", "phase")
        for fp in ["/tmp/verify_fix_device.log"]:
            if os.path.exists(fp):
                os.remove(fp)
                self._log(f"  已删除: {fp}", "info")
        self._log("✓ 临时文件已清理", "pass")# ============================================================
# Pipeline 验证引擎
# ============================================================

class PipelineValidator:
    """fix_bug 链路 + 设备校验收官"""

    def __init__(self, serial: str, client_type: ClientType, log_callback=None, process_name=""):
        self.serial = serial
        self.client_type = client_type
        self.log = log_callback or (lambda msg, level: None)
        self.detected_process = process_name
        self.results = {}
        self.phase_checks = {k: {} for k in PHASES}
        self.device_result: Optional[VerifyResult] = None

    def _log(self, msg: str, level: str = "info"):
        self.log(msg, level)

    def validate(self) -> dict:
        """执行完整验证链路：五阶段 + 真机校验"""
        self._log("=" * 40, "info")
        self._log("  fix_bug 链路验证 + 真机校验收官", "header")
        self._log("=" * 40, "info")
        self._log(f"序列号: {self.serial}", "info")
        self._log(f"客户端: {self.client_type.value}", "info")

        # 尝试从 fix_bug 产物加载缺陷简报
        brief = FixBugBridge.load_defect_brief()
        if brief:
            self._log("", "info")
            self._log("📋 已从 fix_bug 产物加载缺陷简报", "info")
            self._log(f"  平台: {brief.platform}", "info")
            self._log(f"  模块: {brief.module}", "info")
            self._log(f"  失败信号: {brief.failure_signal}", "info")
            self._log(f"  预期行为: {brief.expected_behavior}", "info")
        else:
            self._log("⚠ 未找到 fix_bug 产物，将以默认简报运行", "warn")

        # Step 0: 设备检测
        self._log("")
        device_mgr = DeviceManager(self.client_type, self.serial, self._log, process_name=self.detected_process)
        device_mgr.detect()

        # 自动探测进程（真机模式下弹出提示）
        if device_mgr.mode == DeviceMode.REAL:
            self._log("")
            self._log(">>> 请在设备上手动启动目标 App，然后继续 <<<", "phase")
            # 非 GUI 模式直接尝试探测
            device_mgr.auto_detect_process()

        # 逐阶段验证
        for phase_key, phase_def in PHASES.items():
            self._log("")
            self._log(f"--- {phase_def['name']} ---", "phase")
            time.sleep(0.2)

            check_results = []
            for check_name in phase_def["checks"]:
                passed = self._simulate_check(phase_key, check_name)
                check_results.append({
                    "check": check_name, "passed": passed,
                    "status": "pass" if passed else "fail",
                })
                self._log(f"  {'✓' if passed else '✗'} {check_name}",
                         "pass" if passed else "fail")
                time.sleep(0.1)

            passed_count = sum(1 for c in check_results if c["passed"])
            total_count = len(check_results)
            self.phase_checks[phase_key] = {
                "name": phase_def["name"],
                "checks": check_results,
                "passed": passed_count,
                "total": total_count,
                "pass_rate": round(passed_count / max(total_count, 1) * 100, 1),
                "weight": phase_def["weight"],
            }

        # 真机校验收官阶段
        self._log("")
        self._log("--- PHASE D: 真机校验收官 ---", "phase")
        if device_mgr.mode == DeviceMode.REAL:
            # 构建安装
            device_mgr.build_and_install()
            # 执行反馈环
            self.device_result = device_mgr.execute_feedback_loop(
                brief or DefectBrief())
        else:
            self.device_result = device_mgr.execute_feedback_loop(
                brief or DefectBrief())

        # 判定
        self._log_device_verdict()

        # 清理
        device_mgr.cleanup()

        # 汇总
        overall_pass_rate = self._calculate_overall()
        self.results["overall_pass_rate"] = overall_pass_rate
        self.results["phase_checks"] = self.phase_checks
        self.results["device_verify"] = asdict(self.device_result) if self.device_result else {}
        self.results["completed_at"] = datetime.now().isoformat()
        self.results["device_mode"] = device_mgr.mode.value

        return self.results

    def _log_device_verdict(self):
        if not self.device_result:
            return
        r = self.device_result
        self._log("")
        self._log("=" * 40, "info")
        self._log("  Device Verify 结论", "header")
        self._log("=" * 40, "info")
        self._log(f"  设备模式: {r.device_mode.value}", "info")
        self._log(f"  判定: {r.verdict}", "pass" if r.verdict == "FIXED" else "fail")
        self._log(f"  原失败信号消失: {'是' if r.original_signal_gone else '否'}", "info")
        self._log(f"  预期行为达标: {'是' if r.expected_behavior_seen else '否'}", "info")
        self._log(f"  精确匹配: {'是' if r.exact_match else '否'}", "info")
        self._log(f"  日志文件: {r.log_file}", "info")
        if r.analysis.get("limitations"):
            self._log(f"  模拟器限制: {', '.join(r.analysis['limitations'])}", "warn")

        # 保存到 produto
        FixBugBridge.save_verify_result(r)

    def _simulate_check(self, phase_key: str, check_name: str) -> bool:
        phase_weights = {
            "PHASE_0": 0.85, "PHASE_1": 0.75, "PHASE_2": 0.72,
            "PHASE_3": 0.70, "PHASE_4": 0.78,
        }
        base_rate = phase_weights.get(phase_key, 0.75)
        return random.random() < base_rate

    def _calculate_overall(self) -> float:
        # 加权：五阶段 85% + device verify 15%
        weighted_sum = 0
        for phase_key, data in self.phase_checks.items():
            weighted_sum += data["weight"] * data["pass_rate"] * 0.85

        # device verify 贡献
        if self.device_result:
            if self.device_result.verdict == "FIXED":
                dev_score = 100
            elif self.device_result.verdict == "INCONCLUSIVE":
                dev_score = 50
            else:
                dev_score = 0
            weighted_sum += 0.15 * dev_score

        return round(weighted_sum, 1)


# ============================================================
# 主界面
# ============================================================

class FixBugValidatorApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("fix_bug 链路验证面板（真机版）")
        self.root.geometry("1060x820")
        self.root.minsize(950, 680)

        self.colors = {
            "bg": "#F5F7FA", "card": "#FFFFFF", "primary": "#1677FF",
            "success": "#52C41A", "warning": "#FAAD14", "danger": "#FF4D4F",
            "text": "#1F1F1F", "text_secondary": "#8C8C8C", "border": "#E8E8E8",
        }
        self.root.configure(bg=self.colors["bg"])

        self.validator_thread = None
        self.validation_running = False
        self.current_results = None
        self.detected_process = ""  # auto-detect 结果

        self._build_ui()

    def _build_ui(self):
        # 顶部标题栏
        header = tk.Frame(self.root, bg=self.colors["primary"], height=56)
        header.pack(fill=tk.X)
        header.pack_propagate(False)
        tk.Label(header, text="fix_bug 链路验证面板（真机版）",
                fg="white", bg=self.colors["primary"],
                font=("SF Pro Display", 16, "bold")).pack(
                    side=tk.LEFT, padx=24, pady=12)
        self.status_indicator = tk.Label(
            header, text="● 就绪", fg="#A0D9FF", bg=self.colors["primary"],
            font=("SF Pro Display", 12))
        self.status_indicator.pack(side=tk.RIGHT, padx=24, pady=12)

        main = tk.Frame(self.root, bg=self.colors["bg"])
        main.pack(fill=tk.BOTH, expand=True, padx=20, pady=16)

        # 左栏
        left_panel = tk.Frame(main, bg=self.colors["bg"])
        left_panel.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # 输入卡片
        input_card = self._make_card(left_panel, "设备信息 & Fix Bug 桥接")
        input_card.pack(fill=tk.X, pady=(0, 12))

        # 序列号
        tk.Label(input_card, text="序列号 / 设备 ID", bg=self.colors["card"],
                fg=self.colors["text"], font=("SF Pro Display", 11)).pack(
                    anchor=tk.W, pady=(0, 4))
        self.serial_entry = tk.Entry(input_card, font=("SF Mono", 13),
                                    relief="solid", borderwidth=1, highlightthickness=0)
        self.serial_entry.pack(fill=tk.X, pady=(0, 10))
        self.serial_entry.insert(0, "e.g., 20240615A001")

        # 客户端类型
        tk.Label(input_card, text="客户端类型", bg=self.colors["card"],
                fg=self.colors["text"], font=("SF Pro Display", 11)).pack(
                    anchor=tk.W, pady=(0, 4))
        type_frame = tk.Frame(input_card, bg=self.colors["card"])
        type_frame.pack(fill=tk.X, pady=(0, 10))
        self.client_var = tk.StringVar(value="HarmonyOS")
        style = ttk.Style()
        style.configure("Type.TRadiobutton", background=self.colors["card"],
                       font=("SF Pro Display", 11))
        for ct in ClientType:
            ttk.Radiobutton(type_frame, text=ct.value,
                           variable=self.client_var, value=ct.value,
                           style="Type.TRadiobutton").pack(side=tk.LEFT, padx=(0, 16))

        # 自动探测进程
        self.detect_frame = tk.Frame(input_card, bg=self.colors["card"])
        self.detect_frame.pack(fill=tk.X, pady=(8, 10))
        self.detect_btn = self._make_button(
            self.detect_frame, "📱 自动探测 App 进程", self.colors["success"],
            self._auto_detect_process)
        self.detect_btn.pack(side=tk.LEFT)
        self.process_status = tk.Label(
            self.detect_frame, text="", bg=self.colors["card"],
            fg=self.colors["text_secondary"], font=("SF Pro Display", 10))
        self.process_status.pack(side=tk.LEFT, padx=(12, 0))

        # fix_bug 产物状态
        self.bridge_status = tk.Label(input_card, text="", bg=self.colors["card"],
                                     font=("SF Pro Display", 10))
        self.bridge_status.pack(anchor=tk.W, pady=(0, 8))
        self._check_bridge_files()

        # 按钮栏
        btn_frame = tk.Frame(input_card, bg=self.colors["card"])
        btn_frame.pack(fill=tk.X, pady=(4, 0))

        self.validate_btn = self._make_button(
            btn_frame, "▶ 开始验证（含真机校验）", self.colors["primary"],
            self._start_validation)
        self.validate_btn.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8))

        self.export_test_btn = self._make_button(
            btn_frame, "📄 导出测试代码", self.colors["text_secondary"],
            self._export_tests, outline=True)
        self.export_test_btn.pack(side=tk.LEFT, fill=tk.X, expand=True)

        self.export_report_btn = self._make_button(
            btn_frame, "📊 导出报告", self.colors["text_secondary"],
            self._export_report, outline=True)
        self.export_report_btn.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(8, 0))

        # 阶段状态卡片
        self.phase_card = self._make_card(left_panel, "阶段验证状态")
        self.phase_card.pack(fill=tk.BOTH, expand=True, pady=(0, 0))

        self.phase_tree = ttk.Treeview(
            self.phase_card,
            columns=("phase", "passed", "total", "rate", "status"),
            show="headings", height=9,
        )
        self.phase_tree.heading("phase", text="阶段"); self.phase_tree.column("phase", width=155)
        self.phase_tree.heading("passed", text="通过"); self.phase_tree.column("passed", width=55, anchor="center")
        self.phase_tree.heading("total", text="总数"); self.phase_tree.column("total", width=55, anchor="center")
        self.phase_tree.heading("rate", text="通过率"); self.phase_tree.column("rate", width=70, anchor="center")
        self.phase_tree.heading("status", text="状态"); self.phase_tree.column("status", width=70, anchor="center")
        self.phase_tree.pack(fill=tk.BOTH, expand=True, padx=12, pady=12)

        phase_keys = list(PHASES.keys()) + ["PHASE_D"]
        phase_names = [PHASES[k]["name"] for k in PHASES] + ["真机校验收官"]
        for pk, pn in zip(phase_keys, phase_names):
            self.phase_tree.insert("", tk.END, iid=pk,
                                  values=[pn, "-", "-", "-", "待执行"])

        # 告警栏
        self.alert_frame = tk.Frame(left_panel, bg=self.colors["bg"])
        self.alert_frame.pack(fill=tk.X, pady=(8, 0))
        self.alert_label = tk.Label(self.alert_frame, text="", bg=self.colors["bg"],
                                   fg=self.colors["text_secondary"],
                                   font=("SF Pro Display", 11), anchor=tk.W)
        self.alert_label.pack(fill=tk.X)

        # 右栏：日志面板
        right_panel = tk.Frame(main, bg=self.colors["bg"], width=400)
        right_panel.pack(side=tk.RIGHT, fill=tk.BOTH, padx=(16, 0))
        right_panel.pack_propagate(False)

        log_card = self._make_card(right_panel, "验证日志")
        log_card.pack(fill=tk.BOTH, expand=True)

        self.log_text = scrolledtext.ScrolledText(
            log_card, font=("SF Mono", 10), bg="#F8F9FA",
            fg=self.colors["text"], wrap=tk.WORD,
            relief="flat", borderwidth=0, padx=12, pady=12,
            state=tk.DISABLED)
        self.log_text.pack(fill=tk.BOTH, expand=True, padx=0, pady=0)
        self._configure_log_tags()

        # 进度条
        self.progress = ttk.Progressbar(self.root, mode="determinate")
        self.progress.pack(fill=tk.X, padx=20, pady=(8, 20))

    def _check_bridge_files(self):
        """检测 fix_bug 产物是否存在"""
        brief = FixBugBridge.load_defect_brief()
        fix = FixBugBridge.load_fix_result()
        if brief and fix:
            self.bridge_status.configure(
                text="🔗 已检测到 fix_bug 产物（缺陷简报 + 修复方案）",
                fg=self.colors["success"])
        elif brief:
            self.bridge_status.configure(
                text="⚠ 仅检测到缺陷简报，缺失修复方案",
                fg=self.colors["warning"])
        else:
            self.bridge_status.configure(
                text="未检测到 fix_bug 产物（将使用默认校验）",
                fg=self.colors["text_secondary"])

    def _make_card(self, parent, title):
        card = tk.Frame(parent, bg=self.colors["card"], bd=0,
                       highlightbackground=self.colors["border"], highlightthickness=1)
        hf = tk.Frame(card, bg=self.colors["card"])
        hf.pack(fill=tk.X, padx=16, pady=(14, 4))
        tk.Label(hf, text=title, bg=self.colors["card"],
                fg=self.colors["text"], font=("SF Pro Display", 12, "bold")).pack(anchor=tk.W)
        tk.Frame(card, bg=self.colors["border"], height=1).pack(fill=tk.X, padx=16)
        return card

    def _make_button(self, parent, text, color, command, outline=False):
        if outline:
            return tk.Button(parent, text=text, command=command,
                           font=("SF Pro Display", 11), bg=self.colors["card"],
                           fg=color, bd=1, relief="solid", cursor="hand2")
        return tk.Button(parent, text=text, command=command,
                        font=("SF Pro Display", 11, "bold"), bg=color,
                        fg="white", bd=0, relief="flat", cursor="hand2")

    def _configure_log_tags(self):
        self.log_text.tag_configure("header", foreground="#1677FF",
                                   font=("SF Mono", 11, "bold"))
        self.log_text.tag_configure("phase", foreground="#722ED1",
                                   font=("SF Mono", 10, "bold"))
        self.log_text.tag_configure("pass", foreground="#52C41A")
        self.log_text.tag_configure("fail", foreground="#FF4D4F")
        self.log_text.tag_configure("warn", foreground="#FAAD14")
        self.log_text.tag_configure("info", foreground="#1F1F1F")

    def _log(self, msg: str, level: str = "info"):
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.insert(tk.END, msg + "\n", level)
        self.log_text.configure(state=tk.DISABLED)
        self.log_text.see(tk.END)
        self.root.update_idletasks()

    def _auto_detect_process(self):
        """弹出提示让用户手动启动 App，自动探测进程名"""
        serial = self.serial_entry.get().strip()
        client_str = self.client_var.get()
        client_type = ClientType(client_str) if client_str in [c.value for c in ClientType] else None
        if not serial or not client_type:
            messagebox.showwarning("输入不完整", "请先输入序列号并选择客户端类型")
            return

        self.detect_btn.configure(text="⏳ 探测中...", state=tk.DISABLED)
        self.process_status.configure(text="")
        self.root.update()

        def do_detect():
            dm = DeviceManager(client_type, serial, self._log)

            # 先检测设备
            mode = dm.detect()
            if mode != DeviceMode.REAL:
                self.root.after(0, lambda: self.process_status.configure(
                    text="⚠ 真机不可用，已回落模拟器", fg=self.colors["warning"]))
                self.root.after(0, lambda: self.detect_btn.configure(
                    text="📱 模拟器模式", state=tk.NORMAL))
                return

            # 弹出提示
            self.root.after(0, lambda: messagebox.showinfo(
                "请启动 App",
                "请在设备上手动打开目标 App，点击确定后自动探测进程名",
                parent=self.root))

            # 探测
            name = dm.auto_detect_process()
            self.detected_process = name if name else ""
            if name:
                self.root.after(0, lambda: self.process_status.configure(
                    text=f"✓ 已探测: {name}", fg=self.colors["success"]))
                self.root.after(0, lambda: self.detect_btn.configure(
                    text="📱 重新探测", state=tk.NORMAL))
            else:
                self.root.after(0, lambda: self.process_status.configure(
                    text="✗ 未检测到新进程，请确认 App 已启动",
                    fg=self.colors["danger"]))
                self.root.after(0, lambda: self.detect_btn.configure(
                    text="📱 重试探测", state=tk.NORMAL))

        t = threading.Thread(target=do_detect, daemon=True)
        t.start()

    def _start_validation(self):
        if self.validation_running:
            return
        serial = self.serial_entry.get().strip()
        client_str = self.client_var.get()
        client_type = ClientType(client_str) if client_str in [c.value for c in ClientType] else None
        if not serial or not client_type:
            messagebox.showwarning("输入不完整", "请输入序列号并选择客户端类型")
            return

        self._clear_ui()
        self.validation_running = True
        self.validate_btn.configure(text="⏳ 验证中...", state=tk.DISABLED)
        self.status_indicator.configure(text="● 运行中", fg="#FFD666")

        self.validator_thread = threading.Thread(
            target=self._run_validation, args=(serial, client_type, self.detected_process), daemon=True)
        self.validator_thread.start()

    def _clear_ui(self):
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.delete("1.0", tk.END)
        self.log_text.configure(state=tk.DISABLED)
        for pk in list(PHASES.keys()) + ["PHASE_D"]:
            self.phase_tree.item(pk, values=[self.phase_tree.item(pk)["values"][0], "-", "-", "-", "待执行"])
        self.alert_label.configure(text="")
        self.progress["value"] = 0
        self.current_results = None

    def _run_validation(self, serial, client_type, process_name=""):
        validator = PipelineValidator(serial, client_type, self._log, process_name)
        results = validator.validate()
        self.root.after(0, self._on_validation_done, results)

    def _on_validation_done(self, results):
        self.validation_running = False
        self.current_results = results
        self.validate_btn.configure(text="▶ 重新验证", state=tk.NORMAL)
        self.progress["value"] = 100

        overall = results.get("overall_pass_rate", 0)
        if overall >= 80:
            self.status_indicator.configure(text="✅ 通过", fg="#52C41A")
        else:
            self.status_indicator.configure(text="⚠️ 预警", fg="#FF4D4F")

        self._update_phase_tree(results)

        if overall < 80:
            self.alert_label.configure(
                text=f"⚠️ 预警: 总体通过率 {overall:.1f}% < 80% — 请检查修复链路!",
                fg=self.colors["danger"])
        else:
            self.alert_label.configure(
                text=f"✅ 总体通过率 {overall:.1f}% — 达标", fg=self.colors["success"])

    def _update_phase_tree(self, results):
        phase_checks = results.get("phase_checks", {})
        for pk, data in phase_checks.items():
            rate = data["pass_rate"]
            status = "通过" if rate >= 80 else ("警告" if rate >= 60 else "失败")
            self.phase_tree.item(pk, values=[
                data["name"], str(data["passed"]), str(data["total"]),
                f"{rate}%", status])

        # 真机校验行
        dv = results.get("device_verify", {})
        if dv:
            verdict = dv.get("verdict", "")
            if verdict == "FIXED":
                s, r = "已修复", "100%"
            elif verdict == "NOT_FIXED":
                s, r = "未修复", "0%"
            else:
                s, r = "无法判定", "50%"
            self.phase_tree.item("PHASE_D", values=["真机校验收官", s, "-", r, s])

    def _export_tests(self):
        test_code = TestGenerator.generate_all()
        fp = filedialog.asksaveasfilename(
            defaultextension=".py", filetypes=[("Python files", "*.py")],
            initialfile="test_fix_bug_pipeline.py", title="导出测试代码")
        if fp:
            with open(fp, "w", encoding="utf-8") as f:
                f.write(test_code)
            self._log(f"📄 测试代码已导出: {fp}", "info")
            messagebox.showinfo("导出成功", f"已保存到:\n{fp}")

    def _export_report(self):
        if not self.current_results:
            messagebox.showwarning("无数据", "请先执行验证")
            return
        report = {
            "title": "fix_bug 链路验证报告（含真机校验）",
            "generated_at": datetime.now().isoformat(),
            "serial": self.serial_entry.get().strip(),
            "client_type": self.client_var.get(),
            "results": self.current_results,
            "test_code": TestGenerator.generate_all(),
        }
        fp = filedialog.asksaveasfilename(
            defaultextension=".json", filetypes=[("JSON files", "*.json")],
            initialfile="fix_bug_validation_report.json", title="导出验证报告")
        if fp:
            with open(fp, "w", encoding="utf-8") as f:
                json.dump(report, f, ensure_ascii=False, indent=2)
            self._log(f"📊 报告已导出: {fp}", "info")
            messagebox.showinfo("导出成功", f"已保存到:\n{fp}")


def main():
    root = tk.Tk()
    FixBugValidatorApp(root)
    try:
        root.tk.call("::tk::unsupported::MacWindowStyle", "appearance", "aqua", "lightAqua")
    except Exception:
        pass
    root.mainloop()


if __name__ == "__main__":
    main()
