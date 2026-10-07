# 底盘与充电桩导出件

由 `../chassis.scad`（底盘）与 `../dock.scad`（充电桩）生成，改变量后重新导出即可覆盖。

## 底盘（chassis.scad）

| 文件 | 用途 | 生成命令 |
|---|---|---|
| `chassis_plate.stl` | 十字底板 3D 打印 | `openscad -D 'PART=1' -o chassis_plate.stl ../chassis.scad` |
| `chassis_plate.dxf` | 十字底板激光切割（切铝板） | `openscad -D 'PART=1' -D 'EXPORT_2D=true' -o chassis_plate.dxf ../chassis.scad` |
| `chassis_assembly.stl` | 总装（放仿真/看效果） | `openscad -D 'PART=0' -o chassis_assembly.stl ../chassis.scad` |
| `01_俯视_十字布局.png` | 俯视图（看 L/R 与 90° 布局） | 见下 |
| `02_等轴_总装.png` | 等轴总装 | |
| `03_底板_俯视.png` | 底板零件图 | |
| `04_侧视_分层.png` | 侧视（看电池下挂与轮高） | |

## 充电桩（dock.scad）

| 文件 | 用途 | 生成命令 |
|---|---|---|
| `05_充电桩_等轴.png` | 等轴：看马蹄形开口 + 两侧导引板 | 见下 |
| `06_充电桩_俯视.png` | 俯视：看开口 210mm 与总宽 260mm | |
| `07_充电桩_正视.png` | 正视：看触点柱与红外管高度 | |
| `08_充电桩_侧视.png` | 侧视：看 48mm 低矮高度 | |

```bash
# 桩体零件 STL（打印用）
openscad -D 'PART=1' -o dock_base.stl ../dock.scad
# 总装
openscad -D 'PART=0' -o dock_asm.stl ../dock.scad
# 底板 2D 投影（如切铝）
openscad -D 'PART=2' -o dock_plate.dxf ../dock.scad
```

> 模型自带几何自检（OpenSCAD 控制台会 `ECHO`）：桩体外形、开口余量、
> 导引口外张量、触点间距与**爬电距离**。爬电距离 <8mm 会提示（安全红线）。

## 渲染命令（需 xvfb 或带显示环境）

```bash
# —— 底盘 ——
xvfb-run -a openscad -D 'PART=0' --imgsize=900,900 \
  --camera=0,0,200,0,0,0,1050 --projection=o --render=1 -o 01_俯视_十字布局.png ../chassis.scad

xvfb-run -a openscad -D 'PART=0' --imgsize=1000,780 \
  --camera=0,-40,75,58,0,22,760 --projection=p --render=1 -o 02_等轴_总装.png ../chassis.scad

xvfb-run -a openscad -D 'PART=1' --imgsize=900,900 \
  --camera=0,0,220,0,0,0,560 --projection=o --render=1 -o 03_底板_俯视.png ../chassis.scad

xvfb-run -a openscad -D 'PART=0' --imgsize=1300,600 \
  --camera=0,0,45,90,0,0,470 --projection=o --render=1 -o 04_侧视_分层.png ../chassis.scad

# —— 充电桩（--viewall --autocenter 自动取景，gimbal 角控制视角）——
r() { xvfb-run -a --server-args="-screen 0 1500x1100x24" openscad -o "$1.png" \
  --imgsize=1500,1100 --viewall --autocenter --camera="0,0,0,$2" \
  --projection=p --colorscheme=Tomorrow ../dock.scad; }
r 05_充电桩_等轴 "54,0,26"
r 06_充电桩_俯视 "0,0,0"
r 07_充电桩_正视 "90,0,0"
r 08_充电桩_侧视 "76,0,90"
```

## 切铝板怎么下单

淘宝搜「6061 铝板 激光切割 打孔代加工」，把 `chassis_plate.dxf` 发过去，注明：

- 材料：**6061-T6，4mm 厚**
- 精度：孔位 ±0.2mm，外形 ±0.5mm
- 表面：去毛刺，不做阳极（省 ¥ 且打印件搭接面更有附着力）

> DXF 是**外轮廓 + 孔**的 2D 投影，不含任何 3D 信息。下单前用 LibreCAD / AutoCAD 打开确认比例是 1:1（单位 mm）。

## 充电桩的采购件（打印件之外）

| 件 | 规格 | 参考价 |
|---|---|---|
| 触点弹片 ×2 | 铜镀镍，行程 3mm，载流 ≥5A | ¥8 |
| 红外发射管 ×2 | 5mm 940nm（桩端） | ¥4 |
| 红外接收管 ×2 | 5mm 匹配对管（车尾） | ¥6 |
| 防反接二极管 | SS34 或同规格肖特基 | ¥2 |
| 自恢复保险丝 | 5A 保持电流 | ¥3 |
| 电源适配器 | 12.6V / 4A | ¥45 |
| 连线与接插件 | XT30 / 杜邦 | ¥10 |
| **合计** | （桩体 3D 打印耗材约 ¥15~50） | **≈ ¥80~150** |
