// ============================================================
//  皮皮 · 自动回充桩（DOCK）· OpenSCAD 参数化模型
// ============================================================
//  配套文档：CHASSIS.md §12「自动回充与充电桩」
//
//  设计要点（先读这段，再看代码）：
//    1. 桩体做成「低矮马蹄形」——高度只有 48mm，中间开口让车体钻进去。
//       皮皮是圆柱壳 Ø300 的陪伴机器人，视觉上不该被一个高柜子挡住。
//    2. 对接方向 = 车体「后侧」进入（车头朝外）。
//       理由：车头是云台+摄像头，倒进去不会撞到人脸；且倒车时
//       车头朝外，对接失败时皮皮能立刻"看向"主人做表情。
//    3. 触点弹片装在**竖直面**上（不是地面），靠车体后方的
//       触点座水平顶进去。竖直触点比地面触点更抗灰尘堆积，
//       且麦轮不会压到触点。
//    4. 红外发射管 2 只，装在触点两侧、与触点同高；
//       喇叭口导引槽（两侧斜板）提供 ±35mm 的机械容差,
//       让皮皮只要"大致对准"就能被导正。
//
//  坐标约定：原点在桩体中心的地面上，+Y 指向桩体后方（车体驶入的反方向），
//            +X 朝右，+Z 朝上。车体从 -Y 方向倒进来。
//
//  导出：
//    openscad -D 'PART=1' -o dock_base.stl  hardware/dock.scad   // 桩体
//    openscad -D 'PART=0' -o dock_asm.stl   hardware/dock.scad   // 总装
//    openscad -D 'PART=2' -o dock_plate.dxf hardware/dock.scad   // 底板(投影)
// ============================================================

// ---------------------------------------------------------------- 可调参数
PART = 0;              // [0:总装, 1:桩体主体, 2:底板(2D投影), 3:导引斜板]

// —— 桩体外形
DOCK_W        = 260;   // 桩体总宽（mm）—— 比车体外接圆 Ø300 略窄，
                       // 保证皮皮倒进来时两侧有 20mm 余量不至于擦碰
DOCK_D        = 120;   // 桩体进深（mm）
DOCK_H        = 48;    // 桩体总高（mm）—— 低矮，不挡视线
OPEN_W        = 210;   // 中央开口宽度（mm）—— 车体尾部宽约 150，留 60mm 容差
WALL_T        = 4.0;   // 壁厚（mm）

// —— 触点（竖直面，装在开口最里侧的立板上）
CONTACT_H     = 46;    // 触点高度（离地）—— 车体尾部触点座的高度
CONTACT_DZ    = 22;    // 两触点纵向间距（+12.6V / GND 分开，加大爬电距离）
CONTACT_STUD_D = 10;   // 触点铜柱直径
CONTACT_PROTRUDE = 8;  // 触点弹出长度（mm）—— 弹簧片压缩行程 3mm

// —— 红外发射管座（触点两侧，±40mm）
IR_OFFSET     = 40;    // 红外管距中心距离（mm）
IR_BORE_D     = 5.2;   // 5mm LED 过孔
IR_UP_TILT    = 6;     // 发射管上仰角（度）—— 补偿地面反射

// —— 导引喇叭口（两侧斜板）
FUNNEL_LEN    = 70;    // 斜板长度（mm）
FUNNEL_ANGLE  = 22;    // 张开角（度，单侧）
FUNNEL_T      = 3.0;   // 斜板厚（mm）

// —— 电气件占位（仅用于总装示意，不参与出图）
SHOW_ELECTRONICS = true;
SHOW_CAR         = false;  // 是否画车体轮廓（仅等轴看效果时开）

$fn = 48;

// ---------------------------------------------------------------- 派生量
FUNNEL_X = OPEN_W / 2;                     // 斜板起点 X
FUNNEL_DX = FUNNEL_LEN * sin(FUNNEL_ANGLE);  // 斜板外张的 X 分量
FUNNEL_DY = FUNNEL_LEN * cos(FUNNEL_ANGLE);  // 斜板前伸的 Y 分量
FRONT_Y = -DOCK_D / 2;                     // 桩体前缘 Y
BACK_Y = DOCK_D / 2;                       // 桩体后缘 Y  ← 触点板所在

// ---------------------------------------------------------------- 自检
echo(str("桩体外形 = ", DOCK_W, " × ", DOCK_D, " × ", DOCK_H, " mm"));
echo(str("中央开口宽 = ", OPEN_W, " mm  (车体尾部 150mm → 单侧余量 ",
         (OPEN_W - 150) / 2, " mm)"));
echo(str("导引口外张量 = ", FUNNEL_DX * 2, " mm  (总容差 ±",
         FUNNEL_DX + (OPEN_W - 150) / 2, " mm)"));
echo(str("触点到地面 = ", CONTACT_H, " mm, 触点间距 = ", CONTACT_DZ, " mm"));
echo(str("触点爬电距离 = ", CONTACT_DZ - CONTACT_STUD_D, " mm  (需 >8mm)"));

// ---------------------------------------------------------------- 模块
module rounded_box(w, d, h, r) {
    // 四角倒圆的方块（用 hull + cylinder 实现，兼容 OpenSCAD 2021）
    hull() {
        for (x = [-w/2 + r, w/2 - r])
            for (y = [-d/2 + r, d/2 - r])
                translate([x, y, 0]) cylinder(h = h, r = r);
    }
}

// —— 桩体主体：低矮马蹄形（前方开口，顶部也敞开）
module dock_body() {
    difference() {
        rounded_box(DOCK_W, DOCK_D, DOCK_H, 8);
        // 掏空中央开口：从前方开口一直挖到靠近后缘，**顶部敞开**（+2 保证挖穿）
        translate([0, FRONT_Y - 1, -1])
            cube([OPEN_W, DOCK_D - WALL_T * 2, DOCK_H + 2]);
        // 桩体底面减重槽（省耗材，也给配重块留位）
        translate([0, 0, -1])
            cylinder(h = WALL_T, d = min(DOCK_W, DOCK_D) * 0.72);
        // 后方两个走线孔
        for (x = [-30, 30])
            translate([x, BACK_Y, DOCK_H * 0.5])
                rotate([90, 0, 0]) cylinder(h = WALL_T + 2, d = 8);
        // 底部四角地脚螺丝过孔
        for (x = [-DOCK_W/2 + 18, DOCK_W/2 - 18])
            for (y = [-DOCK_D/2 + 18, DOCK_D/2 - 18])
                translate([x, y, -1]) cylinder(h = DOCK_H + 2, d = 4.2);
    }
}

// 上面那个 translate + center 组合容易错，改用显式块。
module contact_panel() {
    // 立板：从 z=0 到 z=CONTACT_H+18，贴着桩体后缘
    translate([-OPEN_W/2, BACK_Y - WALL_T * 2, 0])
        difference() {
            cube([OPEN_W, WALL_T, CONTACT_H + 18]);
            // 触点铜柱过孔 ×2（纵向排布，靠中心）
            for (dz = [-CONTACT_DZ/2, CONTACT_DZ/2])
                translate([OPEN_W/2, -1, CONTACT_H + dz])
                    rotate([-90, 0, 0])
                        cylinder(h = WALL_T + 2, d = CONTACT_STUD_D + 0.6);
            // 红外发射管过孔 ×2
            for (dx = [-IR_OFFSET, IR_OFFSET])
                translate([OPEN_W/2 + dx, -1, CONTACT_H])
                    rotate([-90, 0, 0])
                        cylinder(h = WALL_T + 2, d = IR_BORE_D);
        }
}

// —— 触点铜柱（弹出状态示意）
module contact_pins() {
    for (dz = [-CONTACT_DZ/2, CONTACT_DZ/2])
        translate([0, BACK_Y - WALL_T * 2, CONTACT_H + dz])
            rotate([-90, 0, 0]) {
                cylinder(h = CONTACT_PROTRUDE, d = CONTACT_STUD_D);
                // 端头圆角
                translate([0, 0, CONTACT_PROTRUDE])
                    sphere(d = CONTACT_STUD_D);
            }
}

// —— 红外发射管
module ir_emitters() {
    for (dx = [-IR_OFFSET, IR_OFFSET])
        translate([dx, BACK_Y - WALL_T * 2, CONTACT_H])
            rotate([-90 + IR_UP_TILT, 0, 0]) {
                // 管体（黑壳）
                cylinder(h = 14, d = 5.0);
                // 引脚
                translate([-1.3, -1.3, -6]) cylinder(h = 6, d = 0.6);
                translate([1.3, -1.3, -6]) cylinder(h = 6, d = 0.6);
            }
}

// —— 一侧导引斜板（用显式坐标多边形，避免旋转方向搞错）
//    铰接端在开口边缘 (±OPEN_W/2, FRONT_Y)，自由端向 -Y 前方 + 向外张开
module funnel_side(sign) {
    x0 = sign * (OPEN_W / 2);
    y0 = FRONT_Y;
    x1 = sign * (OPEN_W / 2 + FUNNEL_DX);
    y1 = FRONT_Y - FUNNEL_DY;
    h = DOCK_H * 0.62;
    // 用 hull 连接两条竖直线段 → 得到一块斜板
    hull() {
        translate([x0, y0, 0]) cylinder(h = h, d = FUNNEL_T);
        translate([x1, y1, 0]) cylinder(h = h, d = FUNNEL_T);
    }
}

module funnel() {
    funnel_side(1);
    funnel_side(-1);
    // 顶部横向连接筋（防止被车体顶弯）—— 横跨两铰接端
    translate([0, FRONT_Y, DOCK_H * 0.62])
        cube([OPEN_W + 2 * FUNNEL_T, 6, 3], center = true);
}

// —— 接地铜片（桩体前缘贴着地面，让车体电极先泄静电）
module ground_strip() {
    translate([0, FRONT_Y + 2, 0.6])
        cube([OPEN_W * 0.7, 12, 1.2], center = true);
}

// —— 电气件占位
module electronics_placeholder() {
    // 充电控制板
    translate([0, BACK_Y - 30, DOCK_H + 2])
        color("green", 0.7) cube([60, 40, 1.6], center = true);
    // DC 输入插座
    translate([DOCK_W/2 - 12, BACK_Y - 10, 14])
        color("dimgray") cube([10, 14, 10], center = true);
}

// —— 车体尾部轮廓（仅示意，不参与加工）
module car_outline() {
    color("steelblue", 0.25) {
        // 车体圆柱 Ø300 的一小段（简化为立方体近似接触面）
        translate([0, -135, 40]) cube([150, 40, 80], center = true);
        // 车尾触点座
        translate([0, BACK_Y - WALL_T * 2 - CONTACT_PROTRUDE - 2, CONTACT_H])
            rotate([-90, 0, 0]) color("goldenrod", 0.9)
                cube([40, 16, 4], center = true);
    }
}

// ---------------------------------------------------------------- 视图
module assembly() {
    dock_body();
    contact_panel();
    funnel();
    ground_strip();
    contact_pins();
    ir_emitters();
    if (SHOW_ELECTRONICS) electronics_placeholder();
    if (SHOW_CAR) car_outline();
}

if (PART == 0) {
    assembly();
} else if (PART == 1) {
    dock_body();
    contact_panel();
} else if (PART == 3) {
    funnel();
} else if (PART == 2) {
    projection(cut = false) dock_body();
} else {
    dock_body();
}
