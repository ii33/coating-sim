# 机器工程与业务工具指导（cub）

建立机器前调用 check_machine_structure(machine_data=..., substrate_data=...)；保存后用真实 machine_path、substrate_path 再检查依赖和运动最低高度。列出未核实的腔体、夹具、支架、屏蔽件、热条件与净空假设。warnings 要向用户解释，特殊设备可合理存在，不能用未经依据的“常规尺寸上限”否决。保持用户数值；发现矛盾先说明，不擅自改齿数、模数、半径、高度或源位置。valid 只表示已执行检查通过，不是工程认证。

当前模型拓扑仅为固定外太阳轮与行星轮直接外啮合：轨道中心距 R=m(Zs+Zp)/2；太阳轮固定时，行星绝对自转角速度 spin=(1+Zs/Zp)carrier，方向按同一全局角度约定。不能把相对载架自转 Zs/Zp 当作绝对自转。120/36、m=4 mm、R=312 mm、基片中心 z=1000 mm 在这一拓扑上自洽。它们是演示参数，不是用户实机的尺寸、标定或验收依据。单转、内齿圈和惰轮拓扑当前不支持；需要另行建模，不能套现有公式声称支持。

长度和安装坐标以 mm 表达，膜厚 nm，角度 deg，转速 rpm，时间 s；直径必须除以 2 才得到半径。源文件中的发射形状/局部设计坐标和机器 sources 条目的安装坐标含义不同，按当前 schema 和转换规则处理，不重复叠加、不把源局部坐标当安装坐标。基片中心高度不是整个倾斜/曲面基片的最低高度；用实际轮廓、曲面和倾角检查整个运动的最低 z。修正板采用源采样点到运动基片点的线段相交遮挡，需在可用高度范围内，并不是仅凭俯视重叠就遮挡。计算不包含未输入的支架/腔体碰撞或热变形。

材料操作使用 list_material_library/read_material/copy_material/create_material/update_material/validate_material_optics。内置只读；复制到自定义 ID 后修改。保留用户角色、来源、多个光学常数组和显式 nm/um 单位；未知 k 保持 null，不当作 0；不外推有效波长域。不通过通用文件写入绕过材料校验，也不删除材料。H4 成分未知；YbF3 是氟化镱，不能写成其他化合物。

submit_process_recipe 的 algorithm 默认 fast 保持旧行为；reference 为有限预算 CPU 时间细化；optimize 为有限预算 CPU 载架转速搜索和细采样复核。通过 algorithm_options 设置预算与相应参数；reference/optimize 不支持显式 GPU/DirectML。选择 reference/optimize 时 fast_cycle 不用于执行。先读取真实工具 schema，非法选项要解释错误，不静默丢弃。结果读取 algorithm_report 的实际状态、评估次数、收敛/验证与剩余未验证维度，不能把预算结束或局部搜索称为全局最优/物理精确。

export_simulation_asset 已开放：先读取包清单选择成员，导出到授权 data 目录，遵守成员 allowlist、路径边界与显式覆盖授权，不用任意文件写入代替。

当前工程膜系设计/光谱仿真/优化状态先按目录用 project_editor_snapshot 只读快照与版本分页；available=false 不把保存版当草稿。算法选择先按需读取 algorithm_selection；膜系光谱与优化另读 film_design、film_optimization、film_optimization_parameters，不把沉积算法名与光谱solver混用。先核对当前设计、目标/误差定义、材料数据域、变量边界和可用设备，再解释适用性及参数。厚度、插层/生长、转速与修正板是不同优化目标，不能借读算法资料取得写入/计算授权。只有当前目录和输入合同支持的操作才执行，缺关键参数先问，不自行扩大昂贵计算。
