from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
import psycopg2
import json
from datetime import datetime
import math
import numpy as np
import statistics

app = Flask(__name__)
CORS(app)

# 数据库配置
DB_CONFIG = {
    "host": "/tmp",
    "port": "5432",
    "database": "beacon_tracking",
    "user": "omm",
    "password": "openGauss@111"
}

def get_db_connection():
    """获取数据库连接"""
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        print("数据库连接成功")
        return conn
    except Exception as e:
        error_msg = f"数据库连接失败: {str(e)}"
        print(error_msg)
        raise Exception(error_msg)

@app.route('/')
def index():
    """提供前端页面"""
    return render_template('index.html')

@app.route('/api/upload', methods=['POST'])
def upload_scan_data():
    """接收OrangePi上报的扫描数据"""
    try:
        data = request.json
        if not data or not all(k in data for k in ['scanner_id', 'beacon_mac', 'rssi']):
            return jsonify({"status": "error", "message": "缺少必要字段"}), 400

        print(f"收到扫描数据: {data}")
        conn = get_db_connection()
        cur = conn.cursor()

        # 插入扫描数据
        cur.execute("""
            INSERT INTO raw_scan_data (scanner_id, beacon_mac, rssi, timestamp)
            VALUES (%s, %s, %s, %s)
        """, (data['scanner_id'], data['beacon_mac'], data['rssi'], datetime.now()))

        conn.commit()
        cur.close()
        conn.close()

        # 触发位置计算
        calculate_position(data['beacon_mac'])
        return jsonify({"status": "success", "message": "数据接收成功"})

    except Exception as e:
        return jsonify({"status": "error", "message": f"数据处理失败: {str(e)}"}), 500

@app.route('/api/positions')
def get_positions():
    """获取每个信标的最新位置数据"""
    try:
        conn = get_db_connection()
        cur = conn.cursor()

        # 查询每个信标的最新位置
        cur.execute("""
            SELECT cp.beacon_mac, cp.x_coord, cp.y_coord, cp.timestamp
            FROM calculated_positions cp
            INNER JOIN (
                SELECT beacon_mac, MAX(timestamp) as max_timestamp
                FROM calculated_positions
                GROUP BY beacon_mac
            ) latest ON cp.beacon_mac = latest.beacon_mac AND cp.timestamp = latest.max_timestamp
            ORDER BY cp.timestamp DESC
        """)

        positions = []
        for row in cur.fetchall():
            positions.append({
                "mac": row[0],
                "x": row[1],
                "y": row[2],
                "timestamp": row[3].isoformat()
            })

        cur.close()
        conn.close()
        print(f"返回 {len(positions)} 个信标的最新位置")
        return jsonify(positions)

    except Exception as e:
        return jsonify({"status": "error", "message": f"获取位置失败: {str(e)}"}), 500

@app.route('/api/history_positions')
def get_history_positions():
    """获取所有历史位置数据（用于显示轨迹）"""
    try:
        conn = get_db_connection()
        cur = conn.cursor()

        # 查询最近100条历史位置数据
        cur.execute("""
            SELECT beacon_mac, x_coord, y_coord, timestamp
            FROM calculated_positions
            ORDER BY timestamp DESC
            LIMIT 100
        """)

        positions = []
        for row in cur.fetchall():
            positions.append({
                "mac": row[0],
                "x": row[1],
                "y": row[2],
                "timestamp": row[3].isoformat()
            })

        cur.close()
        conn.close()
        return jsonify(positions)

    except Exception as e:
        return jsonify({"status": "error", "message": f"获取历史位置失败: {str(e)}"}), 500

def rssi_to_distance(rssi, tx_power=-59, environmental_factor=2.0, calibration_offset=0):
    """
    改进的RSSI转距离模型（进一步调整以适应4×4米小范围）
    Args:
        rssi: 接收信号强度
        tx_power: 1米处的RSSI参考值
        environmental_factor: 环境因子（小范围室内通常2.0-2.5）
        calibration_offset: 校准偏移
    """
    if rssi >= 0:  # RSSI不应该为正数
        return 1.5  # 在更小范围内，返回更小的默认距离

    try:
        # 使用对数距离路径损耗模型
        distance = 10 ** ((tx_power - rssi) / (10 * environmental_factor))

        # 添加校准偏移
        distance += calibration_offset

        # 限制最小和最大距离（适应4×4米小范围）
        distance = max(0.2, min(distance, 4.0))  # 最大距离限制为4米

        return distance
    except Exception as e:
        print(f"RSSI转距离计算错误: {e}, RSSI: {rssi}")
        return 1.0  # 在更小范围内，返回更小的默认距离

def weighted_centroid(scanners):
    """加权质心算法（确保始终返回值）"""
    total_x, total_y, total_weight = 0, 0, 0

    for scanner in scanners:
        # 使用距离的倒数作为权重（距离越近权重越大）
        weight = 1.0 / (scanner['distance'] + 0.1)
        total_x += scanner['x'] * weight
        total_y += scanner['y'] * weight
        total_weight += weight

    if total_weight > 0:
        return total_x / total_weight, total_y / total_weight
    else:
        # 如果权重总和为0，返回第一个扫描器的位置作为默认值
        if len(scanners) > 0:
            return scanners[0]['x'], scanners[0]['y']
        else:
            return 2.0, 2.0  # 返回区域中心作为默认值

def trilateration_least_squares(scanners):
    """
    使用最小二乘法进行三边定位
    scanners: 列表，每个元素包含 {'x', 'y', 'distance'}
    """
    try:
        if len(scanners) < 3:
            print("三边定位需要至少3个扫描器")
            return weighted_centroid(scanners)  # 降级到加权质心

        # 转换为numpy数组便于计算
        points = np.array([[s['x'], s['y']] for s in scanners])
        distances = np.array([s['distance'] for s in scanners])

        # 使用第一个点作为参考点
        A = []
        b = []

        for i in range(1, len(scanners)):
            # 构建线性方程组 Ax = b
            xi, yi = points[i]
            x1, y1 = points[0]
            di = distances[i]
            d1 = distances[0]

            A.append([2*(xi - x1), 2*(yi - y1)])
            b.append([xi**2 - x1**2 + yi**2 - y1**2 + d1**2 - di**2])

        A = np.array(A)
        b = np.array(b)

        # 最小二乘解
        result = np.linalg.lstsq(A, b, rcond=None)
        if len(result[0]) == 2:
            x, y = result[0].flatten()
            return float(x), float(y)
        else:
            print("三边定位最小二乘解失败")
            return weighted_centroid(scanners)

    except Exception as e:
        print(f"三边定位计算错误: {e}")
        # 降级到加权质心算法
        return weighted_centroid(scanners)

def smooth_position(beacon_mac, new_x, new_y):
    """对位置进行平滑滤波"""
    try:
        conn = get_db_connection()
        cur = conn.cursor()

        # 获取最近3个位置进行平均
        cur.execute("""
            SELECT x_coord, y_coord FROM calculated_positions
            WHERE beacon_mac = %s
            ORDER BY timestamp DESC
            LIMIT 3
        """, (beacon_mac,))

        history_positions = cur.fetchall()

        if len(history_positions) >= 2:
            # 加权平均：新位置权重0.6，历史位置平均权重0.4
            avg_x = sum(pos[0] for pos in history_positions) / len(history_positions)
            avg_y = sum(pos[1] for pos in history_positions) / len(history_positions)

            smoothed_x = new_x * 0.6 + avg_x * 0.4
            smoothed_y = new_y * 0.6 + avg_y * 0.4

            return smoothed_x, smoothed_y
        else:
            return new_x, new_y

    except Exception as e:
        print(f"位置平滑错误: {e}")
        return new_x, new_y
    finally:
        try:
            cur.close()
            conn.close()
        except:
            pass  # 忽略关闭连接时的错误

def calculate_position(beacon_mac):
    """改进的信标位置计算（三边定位+滤波）"""
    try:
        conn = get_db_connection()
        cur = conn.cursor()

        # 获取该信标最近15秒内的记录，按时间倒序
        sql_query = """
            SELECT scanner_id, rssi, location_x, location_y
            FROM raw_scan_data rd
            JOIN scanners s ON rd.scanner_id = s.id
            WHERE beacon_mac = %s
            AND timestamp >= NOW() - INTERVAL '15 seconds'
            ORDER BY timestamp DESC
        """

        cur.execute(sql_query, (beacon_mac,))
        scans = cur.fetchall()

        print(f"计算位置：信标 {beacon_mac} 最近15秒内扫描记录数: {len(scans)}")

        # 对每个扫描器的数据进行分组和平均（减少噪声）
        scanner_data = {}
        for scan in scans:
            scanner_id, rssi, x, y = scan
            if scanner_id not in scanner_data:
                scanner_data[scanner_id] = {'rssi_values': [], 'x': x, 'y': y}
            scanner_data[scanner_id]['rssi_values'].append(rssi)

        # 计算每个扫描器的平均RSSI（去除极端值）
        valid_scanners = []
        for scanner_id, data in scanner_data.items():
            if len(data['rssi_values']) >= 1:
                # 使用中位数减少异常值影响
                sorted_rssi = sorted(data['rssi_values'])
                median_rssi = sorted_rssi[len(sorted_rssi) // 2]

                # 数据质量检查
                if -80 < median_rssi < -20:  # 合理的RSSI范围
                    distance = rssi_to_distance(median_rssi, environmental_factor=2.0)
                    valid_scanners.append({
                        'x': data['x'],
                        'y': data['y'],
                        'distance': distance,
                        'rssi': median_rssi
                    })
                    print(f"扫描器{scanner_id}: 位置({data['x']},{data['y']}), RSSI中位数{median_rssi}, 估计距离{distance:.2f}m")

        # 初始化位置变量
        estimated_x = None
        estimated_y = None

        # 根据有效扫描器数量选择定位算法
        if len(valid_scanners) == 0:
            print(f"信标 {beacon_mac} 没有有效扫描器数据")
            cur.close()
            conn.close()
            return

        elif len(valid_scanners) == 1:
            # 单个扫描器：在扫描器周围随机分布（模拟粗略定位）
            scanner = valid_scanners[0]
            import random
            angle = random.uniform(0, 2 * math.pi)
            offset_distance = scanner['distance'] * 0.3  # 在距离的30%范围内
            estimated_x = scanner['x'] + offset_distance * math.cos(angle)
            estimated_y = scanner['y'] + offset_distance * math.sin(angle)
            print(f"单个扫描器模式：在扫描器({scanner['x']},{scanner['y']})周围{offset_distance:.2f}m生成位置")

        elif len(valid_scanners) == 2:
            # 两个扫描器：使用加权质心
            estimated_x, estimated_y = weighted_centroid(valid_scanners)
            if estimated_x is not None and estimated_y is not None:
                print(f"两个扫描器模式：使用加权质心算法，位置({estimated_x:.2f}, {estimated_y:.2f})")
            else:
                print(f"两个扫描器模式：加权质心计算失败")
                cur.close()
                conn.close()
                return

        else:
            # 三个及以上扫描器：使用三边定位
            estimated_x, estimated_y = trilateration_least_squares(valid_scanners)
            if estimated_x is not None and estimated_y is not None:
                print(f"三个扫描器模式：使用三边定位算法，位置({estimated_x:.2f}, {estimated_y:.2f})")
            else:
                print(f"三个扫描器模式：三边定位计算失败")
                cur.close()
                conn.close()
                return

        # 只有成功计算位置时才继续
        if estimated_x is not None and estimated_y is not None:
            # 位置平滑
            smoothed_x, smoothed_y = smooth_position(beacon_mac, estimated_x, estimated_y)

            # 边界检查
            smoothed_x = max(0, min(4, smoothed_x))  # x范围0-4
            smoothed_y = max(0, min(4, smoothed_y))   # y范围0-4

            # 插入计算结果
            cur.execute("""
                INSERT INTO calculated_positions (beacon_mac, x_coord, y_coord)
                VALUES (%s, %s, %s)
            """, (beacon_mac, round(smoothed_x, 2), round(smoothed_y, 2)))

            conn.commit()
            print(f"信标 {beacon_mac} 位置计算完成: 原始({estimated_x:.2f}, {estimated_y:.2f}) -> 平滑({smoothed_x:.2f}, {smoothed_y:.2f})")
        else:
            print(f"信标 {beacon_mac} 位置计算失败")

        cur.close()
        conn.close()

    except Exception as e:
        error_msg = f"位置计算错误: {str(e)}"
        print(error_msg)
        import traceback
        print(f"异常详情: {traceback.format_exc()}")

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
