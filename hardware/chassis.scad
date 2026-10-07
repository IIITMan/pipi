// =============================================================================
//  皮皮 · 真·十字形麦克纳姆轮底盘（参数化模型）
//  ---------------------------------------------------------------------------
//  用法：
//    1. 装 OpenSCAD（免费，https://openscad.org/）
//    2. 改下面 "★ 可调参数" 区块里的数字（单位 mm）
//    3. F5 预览 → F6 渲染 → F7 导出 .stl / .dxf
//    4. 送 3D 打印（PETG 或 ABS，填充 40%，壁厚 ≥ 1.6mm）或按 DXF 切铝板
//
//  设计文档见 /workspace/CHASSIS.md（几何推导、电池选型、装配顺序）
//  渲染视图：按 0-9 数字键切换（见文末 PART 索引）
// =============================================================================

/* [★ 可调参数：底盘几何] */
// 轮距（左右轮心距）—— 十字布局中必须 = 轴距
TRACK            = 240;    // [180:10:320]
// 轴距（前后轮心距）—— 十字布局要求与 TRACK 相等
WHEELBASE        = 240;    // [180:10:320]
// 中心枢纽半径（要能容下 C30D 板 + 电池架）
HUB_R            = 62;     // [50:2:80]
// 十字臂宽度（电机支架安装面）
ARM_W            = 58;     // [40:2:80]
// 底板厚度
PLATE_T          = 4.0;    // [3:0.5:8]
// 电池仓下挂深度（电池挂在底板下方，降重心）
BATT_DROP        = 26;     // [18:2:40]

/* [★ 可调参数：电机与麦轮] */
// 麦轮标称直径
WHEEL_D          = 60;     // [48:2:100]
// 轮毂宽（不含辊子）
WHEEL_W          = 31;     // [20:1:50]
// 单侧辊子外凸量（决定相邻轮最小间距）
ROLLER_BULGE     = 9;      // [6:1:14]
// 电机安装孔：MG513 减速箱 4×M3，孔距
MOTOR_HOLE_PITCH = 25;     // 常见 GB37/MG513 支架为 25 或 30
MOTOR_HOLE_D     = 3.4;
// 轮轴中心相对底板下表面的高度 = WHEEL_D/2
AXLE_Z           = WHEEL_D/2;

/* [★ 可调参数：电池仓（见 CHASSIS.md §5 选型表）] */
// "18650" / "21700" / "26650"
BATT_CELL        = "18650";
// 并联节数 P（串联固定 3S）
BATT_P           = 2;      // [2:1:3]
// 支架壁厚（单边）
BATT_WALL        = 3;
// 电芯间散热/装配间隙
BATT_DELTA       = 2;

/* [渲染选项] */
SHOW_WHEELS      = true;
SHOW_BATTERY     = true;
SHOW_MOTORS      = true;
SHOW_TIER2       = false;  // 上层隔板示意
$fn              = 48;

// =============================================================================
//  派生参数（不用手改）
// =============================================================================

// 相邻轮轴心距（十字：90° 夹角 → 对角线 = 边长 × √2）
ADJ_DIAG   = sqrt(pow(TRACK/2, 2) + pow(WHEELBASE/2, 2));
// 相邻轮最小轴心距需求
ADJ_MIN    = WHEEL_W + 2*ROLLER_BULGE + 20;

// 电芯尺寸表
function cell_d(c) = c == "21700" ? 21.2 : (c == "26650" ? 26.5 : 18.4);
function cell_h(c) = c == "21700" ? 70.4 : (c == "26650" ? 65.4 : 65.2);
CELL_D   = cell_d(BATT_CELL);
CELL_H   = cell_h(BATT_CELL);
// 电池包尺寸：3S（3 节串成一条）× P 列并排
// 电芯间隙 BATT_DELTA 用于散热与装配容差
PACK_W   = CELL_D * BATT_P + BATT_DELTA * (BATT_P - 1) + 2*BATT_WALL;
PACK_H   = CELL_D * 3     + BATT_DELTA * 2            + 2*BATT_WALL;
PACK_L   = CELL_H + 8;                                 // 电芯轴向 + 上下壁

// 中心枢纽圆盘半径：至少要容下电池包
HUB_R_ACT = max(HUB_R, PACK_H/2 + 8, PACK_L/2 + 8);

// 轮心位置（俯视，原点在底盘中心；+X 前，+Y 左）
// ★ 十字布局关键：轮心在四条臂的**末端**，即坐标轴上，不在四角。
//   这才是「十字」—— 相邻两轮连线与中心成 90°，轮心距 = TRACK/√2。
function wheel_pos(i) =
    i == 0 ? [ TRACK/2,      0] :   // 前轮（+X 臂端）
    i == 1 ? [ 0,       WHEELBASE/2] :   // 左轮（+Y 臂端）
    i == 2 ? [-TRACK/2,      0] :   // 后轮（−X 臂端）
             [ 0,      -WHEELBASE/2];   // 右轮（−Y 臂端）

// 麦轮左右旋：X 型标准 L-R-R-L（对角同向、相邻反向）
//   前↔后 同向（都 L）｜ 左↔右 同向（都 R）
function wheel_hand(i) = (i == 0 || i == 2) ? "L" : "R";

// 每条臂的朝向角（0° 指 +X；逆时针 90° 指 +Y）
function arm_angle(i) = (i == 0) ? 0   :
                        (i == 1) ? 90  :
                        (i == 2) ? 180 : 270;

// =============================================================================
//  几何自检（OpenSCAD 控制台会打印）
// =============================================================================
echo(str("相邻轮轴心距 = ", ADJ_DIAG, " mm  (最小需求 ", ADJ_MIN, " mm)"));
echo(str(ADJ_DIAG >= ADJ_MIN ? "  ✅ 轮子不干涉" : "  ❌ 轮子干涉！请增大 TRACK/WHEELBASE"));
echo(str("车体外接圆直径 = ", TRACK + WHEEL_D, " mm"));
echo(str("电池包尺寸 = ", PACK_W, " × ", PACK_L, " × ", PACK_H, " mm"));
echo(str("枢纽半径实取 = ", HUB_R_ACT, " mm"));

// =============================================================================
//  模块定义
// =============================================================================

// ---- 中心枢纽圆盘 ----
module hub(hole_ring = true) {
    difference() {
        cylinder(h = PLATE_T, r = HUB_R_ACT);
        // 中心大减重孔（穿线 + 减重）
        translate([0, 0, -1]) cylinder(h = PLATE_T + 2, r = HUB_R_ACT - 22);
        // 周边 M3 固定孔
        if (hole_ring)
            for (a = [0 : 45 : 315])
                rotate([0, 0, a])
                    translate([HUB_R_ACT - 12, 0, -1])
                        cylinder(h = PLATE_T + 2, d = 3.4);
    }
}

// ---- 单条十字臂（从中心指向 +X，含端部电机座）----
module arm() {
    len   = TRACK/2 - HUB_R_ACT + 10;    // 与枢纽重叠 10mm 保证融合
    plate = [len, ARM_W, PLATE_T];
    translate([HUB_R_ACT - 10, 0, 0]) {
        difference() {
            // 臂主体（端部圆角，用圆柱收边）
            union() {
                translate([0, -ARM_W/2, 0]) cube(plate);
                translate([len, 0, 0]) cylinder(h = PLATE_T, r = ARM_W/2);
            }
            // 沿臂的减重槽（两条）
            for (sy = [-1, 1])
                translate([len*0.18, sy*ARM_W*0.22 - 4, -1])
                    cube([len*0.55, 8, PLATE_T + 2]);
            // 端部电机轴过孔（轮心 = TRACK/2，即本臂末端外侧）
            translate([len, 0, -1]) cylinder(h = PLATE_T + 2, d = 12);
        }
        // 电机安装 M3 孔（4 个）
        translate([len, 0, -1])
            for (sx = [-1, 1], sy = [-1, 1])
                translate([sx*MOTOR_HOLE_PITCH/2, sy*MOTOR_HOLE_PITCH/2, 0])
                    cylinder(h = PLATE_T + 2, d = MOTOR_HOLE_D);
    }
}

// ---- 十字底板（4 条臂 + 枢纽一体）----
module cross_plate() {
    union() {
        hub();
        for (a = [0 : 90 : 270]) rotate([0, 0, a]) arm();
    }
}

// ---- 麦轮（示意：轮毂 + 45° 辊子）----
module wheel(hand = "L") {
    rotate([0, 0, 90]) {                    // 轮轴沿 Y（车体左右）
        // 轮毂
        difference() {
            cylinder(h = WHEEL_W, r = WHEEL_D/2 - ROLLER_BULGE, center = true);
            cylinder(h = WHEEL_W + 2, d = 6, center = true);
        }
        // 辊子：倾斜 45°，左旋 / 右旋 镜像
        sgn = (hand == "L") ? 1 : -1;
        for (i = [0 : 45 : 315])
            rotate([0, 0, i])
                rotate([sgn * 45, 0, 0])
                    translate([WHEEL_D/2 - ROLLER_BULGE, 0, 0])
                        rotate([0, 90, 0])
                            cylinder(h = WHEEL_W + 2, r = ROLLER_BULGE, center = true);
    }
}

// ---- 电机 + 减速箱（示意）----
module motor_body() {
    // 减速箱
    translate([0, 0, -AXLE_Z])
        rotate([0, 90, 0])
            cylinder(h = 20, r = 17, center = true);
    // 电机本体（朝车体内侧）
    translate([-10, 0, -AXLE_Z])
        rotate([0, 90, 0])
            cylinder(h = 32, r = 21, center = true);
    // 编码器尾
    translate([-28, 0, -AXLE_Z])
        rotate([0, 90, 0])
            cylinder(h = 14, r = 12, center = true);
}

// ---- 电池包（3S×P + 支架）----
module battery_pack() {
    color([0.15, 0.35, 0.75]) {
        // 支架外壳
        translate([0, 0, -BATT_DROP])
            cube([PACK_W, PACK_L, PACK_H], center = true);
        // 电芯（内缩显示）：W 方向 P 列、H 方向 3 节串联
        for (i = [0 : BATT_P - 1], j = [0 : 2])
            translate([-PACK_W/2 + BATT_WALL + CELL_D/2 + i*(CELL_D + BATT_DELTA),
                       0,
                       -BATT_DROP - PACK_H/2 + BATT_WALL + CELL_D/2 + j*(CELL_D + BATT_DELTA)])
                cylinder(h = CELL_H, r = CELL_D/2, center = true);
    }
}

// ---- 上层隔板（示意，用于云台与主控）----
module tier2_plate() {
    z = 78;
    color([0.85, 0.85, 0.88])
        difference() {
            translate([0, 0, z]) cylinder(h = 3, r = HUB_R_ACT + 6);
            translate([0, 0, z - 1]) cylinder(h = 5, d = 40);
        }
    // 四根支撑铜柱
    color([0.6, 0.6, 0.65])
        for (a = [45 : 90 : 315])
            rotate([0, 0, a])
                translate([HUB_R_ACT - 6, 0, 0])
                    cylinder(h = z, d = 6);
}

// =============================================================================
//  装配
// =============================================================================
module assembly() {
    color([0.25, 0.28, 0.32]) cross_plate();

    if (SHOW_MOTORS)
        for (i = [0 : 3])
            rotate([0, 0, arm_angle(i)])
                translate([TRACK/2, 0, 0]) motor_body();

    if (SHOW_WHEELS)
        for (i = [0 : 3])
            rotate([0, 0, arm_angle(i)])
                translate([TRACK/2, 0, -AXLE_Z]) wheel(wheel_hand(i));

    if (SHOW_BATTERY) battery_pack();

    if (SHOW_TIER2) tier2_plate();
}

// =============================================================================
//  视图索引（按 0-9 切换）
// =============================================================================
PART = 0;   // [0:总装, 1:十字底板, 2:单臂, 3:枢纽, 4:麦轮L, 5:麦轮R, 6:电池包, 7:电机]
// 导出 DXF（切铝板用）：把 EXPORT_2D 设为 true
EXPORT_2D = false;

if (EXPORT_2D) {
    // 2D 投影：给激光切割/水刀用
    projection(cut = false)
        if (PART == 1) cross_plate();
        else assembly();
} else {
    if      (PART == 0) assembly();
    else if (PART == 1) color([0.25, 0.28, 0.32]) cross_plate();
    else if (PART == 2) color([0.25, 0.28, 0.32]) arm();
    else if (PART == 3) color([0.25, 0.28, 0.32]) hub();
    else if (PART == 4) wheel("L");
    else if (PART == 5) wheel("R");
    else if (PART == 6) battery_pack();
    else if (PART == 7) motor_body();
}
