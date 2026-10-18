# 数据结构 质量控制与复用说明

original_url: https://github.com/ii33/coating-research-library/blob/d1acac3766a3a1f0a14064edcbeadb7ea4244f4e/04_coating_and_substrate_materials/reports/03_data_quality_and_validation.md
来源版本：d1acac3766a3a1f0a14064edcbeadb7ea4244f4e。原文中“本轮”“本机”“代码”等指来源研究的范围，不自动代表本软件已实现或已验证。
权利：按用户要求收录项目原创研究文字；来源未声明全库许可证，本导入不改授软件 MIT。第三方原文/图片未内置。
阅读：相对 Markdown 链接改为固定版本在线来源；图片改为原图链接。正文中的源码、数据路径及复算命令仅供阅读，附件与可执行代码未随此快照收录。


## 1 文件层次

- `data/materials_manifest.json`：16个原文优先对象，按原顺序，膜/基片分ID
- `data/source_registry.json`：合并来源目录，保留原组字段，所有结论可回溯原入口和边界
- `data/property_records.json`：条件化参数示例；不是完整材料常数数据库，缺值用null
- `data/property_record.schema.json`：供以后导入更多数据的规范
- `data/metal_nk_derived_points.json`：五组CC0光学参考数据的指定波长插值；保留源SHA256
- `data/local_originals_manifest.json`：合法CC0原始数据本地文件的字节/哈希/来源/版权记录
- `data/worked_examples.json`：有假设边界的可重算光学例
- `cards/`：按原文顺序独立整理的16张卡，末尾附本卡证据链接

源PDF/厂商图片未整体上传到公开研究包。`sources/originals/`全部原件默认本地排除；CC0原始数据也遵从本项目的原件本地策略。数据集CC0不扩大到原论文全文的版权。摘要与本库推导为原创表达；精确数字保留其出处与适用条件。

## 2 核心字段解释

每条数值记录至少需要：record_id、material_id、property、value、unit、state、conditions、source_id、uncertainty、status。字符串区间和不等号不能随意丢掉：例如k检出限、α上限、产品透过率下限要用qualifier与bound_type。具体牌号应放grade字段，source_material和film_state分开。

conditions是结构化对象：wavelength_um、temperature_K或温区、thickness_nm/mm、deposition_method、substrate、anneal、measurement、atmosphere、orientation、notes。未报告值明写null/unknown，不能凭“室温”写成精确293.15K；温度范围不可误作标准差。

任何数据进入设计系统前都需经过状态筛选：

1. source_id确实存在，来源发布日期/访问日期及可读范围清楚
2. 原材料身份、状态和指标定义与目标匹配；牌号缺失时为候选
3. 单位与谱域自洽；密度、压力、硬度及α换算保留原单位
4. 谱表按λ严格递增；被动约定k≥0；重复波长不能覆盖原值
5. 数值未知不等于0；只列n的数据不能自动设k=0
6. 误差的类型明确：测量、目录典型、模型残差或跨来源情景差
7. 冲突数据保留，quarantined状态默认禁止设计读取
8. 变更形成新版本，旧设计继续能重现其使用的冻结输入

## 3 源选择规则

不设一个“永远优于其他源”的总排名。对本机预测，匹配工艺与测量条件且通过留出验证的数据优先；对来料身份，原厂COA/SDS优先；对基础机理，原始研究优先。原厂技术页可以很有用，也可能缺误差/温度或存在排版错误。论文精确值也可能来自不同基片、薄膜厚度、晶相和工艺，不能只按发表级别裁决。

## 4 本轮数值范围

数值脚本只实现单一数据集内线性插值、正入射半无限表面R、内部吸收长度、非相干平板R/T/A及一个明确标记的Lorentz–Lorenz孔隙教学例。它不是TMM、热机耦合或生产优化器。

运行（从专题目录）：

```sh
python models/optical_examples.py
python -m unittest discover -s tests -v
```

公开包可直接用衍生点重算示例；完整插值重建需要本地原件，`models/build_nk_points.py`会验证哈希后再生成，缺原件时明确失败而不取别的版本。原件可由manifest给定URL自行取得；本库不在测试时暗中联网。

## 5 哪些结论已经被检验

自动QA检查对象顺序、源ID、必要字段、缺值状态、谱表单调/被动性、外推拒绝、公式极限和能量守恒。能量守恒只检验实现与定义是否一致，不证明真实窗口无散射或来源数据准确。

叙述QA检查16个优先项、H4/YbF3/K9身份、薄膜/块材分离、数据冲突、SDS与版权边界。它属于本轮编写者自检和交叉核对，不冒充用户指定完整模型审查链。独立审查状态由项目的阶段review单独记录。

实测验证未做；所有生产性能、实际源—膜关联和具体合格判定均未验收。T05可以开始，但材料实测待办应继续可追踪。
