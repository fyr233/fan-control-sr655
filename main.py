#!/usr/bin/env python3
import subprocess
import json
import time
import datetime
from typing import Dict, List, Tuple

# --------------------------
# 用户配置区域（根据实际情况修改）
# --------------------------

# 传感器映射（逻辑名称 -> [芯片名, 特征组, 字段名]）
SENSOR_MAPPING = {
    'cpu': ('k10temp-pci-00c3', 'Tctl', 'temp1_input'),
    'gpu0': ('amdgpu-pci-0300', 'junction', 'temp2_input'), # slot 7
    'gpu1': ('amdgpu-pci-8300', 'junction', 'temp2_input'), # slot 4
    'nvme1': ('nvme-pci-4200', 'Composite', 'temp1_input'),
    'nvme2': ('nvme-pci-4100', 'Composite', 'temp1_input')
}

# 风扇配置（风扇ID -> 关注的传感器列表）
FAN_CONFIG = {
    1: ['gpu0'],
    2: ['gpu0', 'gpu1'], 
    3: ['gpu1'], 
    4: ['gpu1', 'cpu'], 
    5: ['cpu'], 
    6: ['cpu'] # 风扇6只监控CPU温度
}

# 转速控制分段线性曲线（温度℃, 转速百分比），按温度升序排列
SPEED_CURVE = [
    (40, 5),   # ≤40℃: 30%
    (50, 8),   
    (60, 15),
    (70, 40),
    (80, 50),
    (90, 60),
    (95, 100)   # ≥95℃: 100%
]

# 检测间隔（秒）
INTERVAL = 5

# IPMI命令模板（十六进制参数）
IPMI_TEMPLATE = 'sudo ipmitool raw 0x3c 0x30 0x00 {fan_id} {speed}'

# --------------------------
# 功能函数（通常无需修改）
# --------------------------

class ConfigError(Exception):
    """自定义配置异常"""
    pass

def get_sensor_data() -> Dict:
    """获取传感器JSON数据"""
    try:
        result = subprocess.run(['sensors', '-j'], check=True, 
                              stdout=subprocess.PIPE, text=True)
        return json.loads(result.stdout)
    except (subprocess.CalledProcessError, json.JSONDecodeError) as e:
        raise RuntimeError(f"传感器数据获取失败: {str(e)}")

def parse_temperatures(sensor_data: Dict) -> Dict[str, float]:
    """从传感器数据中提取配置的温度值"""
    temps = {}
    for name, (chip, feature, field) in SENSOR_MAPPING.items():
        try:
            value = sensor_data[chip][feature][field]
            temps[name] = float(value)
        except KeyError as e:
            missing_path = []
            if chip not in sensor_data:
                missing_path.append(f"芯片 '{chip}'")
            elif feature not in sensor_data[chip]:
                missing_path.append(f"特征组 '{feature}'")
            else:
                missing_path.append(f"字段 '{field}'")
            raise ConfigError(
                f"传感器 '{name}' 配置错误：未找到 {' → '.join(missing_path)}"
            ) from e
    return temps

def calculate_speed(temp: float) -> int:
    """分段线性插值计算转速百分比"""
    sorted_curve = sorted(SPEED_CURVE, key=lambda x: x[0])
    
    # 处理边界情况
    if temp <= sorted_curve[0][0]:
        return sorted_curve[0][1]
    if temp >= sorted_curve[-1][0]:
        return sorted_curve[-1][1]
    
    # 寻找温度区间
    for i in range(1, len(sorted_curve)):
        (t0, s0), (t1, s1) = sorted_curve[i-1], sorted_curve[i]
        if t0 <= temp <= t1:
            ratio = (temp - t0) / (t1 - t0)
            return int(round(s0 + ratio * (s1 - s0)))
    
    return sorted_curve[0][1]  # 理论上不可达

def set_fan_speed(fan_id: int, speed_pct: int):
    """通过IPMI设置风扇转速"""
    if not 0 <= speed_pct <= 100:
        raise ValueError(f"无效转速值: {speed_pct}%")
    
    # 转换为两位十六进制（保留0x前缀以增强可读性）
    cmd = IPMI_TEMPLATE.format(
        fan_id=f"0x{fan_id:02x}",
        speed=f"0x{speed_pct:02x}"
    )
    
    try:
        # 同时捕获stdout和stderr，避免终端换行
        result = subprocess.run(
            cmd.split(),
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        
        # 添加带毫秒的时间戳
        print(f"风扇{fan_id} → {speed_pct}%", end='  ')
        
    except subprocess.CalledProcessError as e:
        # 错误信息换行显示并包含详细诊断
        error_msg = e.stderr.strip() or result.stdout.strip()
        print(f"\n[{timestamp}] 设置失败 风扇{fan_id}: {cmd}\n    错误详情: {error_msg}")

def validate_config(sensor_data: Dict):
    """配置完整性验证"""
    # 检查风扇配置中的传感器名称
    for fan_id, sensors in FAN_CONFIG.items():
        for s in sensors:
            if s not in SENSOR_MAPPING:
                raise ConfigError(
                    f"风扇{fan_id}配置了未定义的传感器 '{s}'，"
                    "请检查FAN_CONFIG和SENSOR_MAPPING"
                )
    
    # 检查实际传感器数据
    temps = parse_temperatures(sensor_data)
    for fan_id, sensors in FAN_CONFIG.items():
        for s in sensors:
            if s not in temps:
                raise ConfigError(
                    f"风扇{fan_id}的传感器 '{s}' 未能获取数据，"
                    "请检查硬件连接和配置"
                )

def main_loop():
    """主控制循环"""
    try:
        # 初始硬件检测
        sensor_data = get_sensor_data()
        validate_config(sensor_data)
    except Exception as e:
        print(f"配置验证失败: {str(e)}")
        print("请根据错误信息检查配置文件")
        return

    while True:
        try:
            current_temps = parse_temperatures(get_sensor_data())
            

            timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            print(timestamp, end='  ')
            for fan_id, sensors in FAN_CONFIG.items():
                # 获取相关传感器温度
                relevant_temps = [current_temps[s] for s in sensors]
                target_speed = calculate_speed(max(relevant_temps))
                set_fan_speed(fan_id, target_speed)
            print('')
            
            time.sleep(INTERVAL)
            
        except KeyboardInterrupt:
            print("\n脚本已安全终止")
            break
        except Exception as e:
            print(f"运行时错误: {str(e)}")
            time.sleep(INTERVAL)

if __name__ == "__main__":
    main_loop()