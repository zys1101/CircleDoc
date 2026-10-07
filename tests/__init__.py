"""tests —— pytest 测试包。

放测试的目录约定：本包由 circledoc-qa 维护（PRD FR-406 要求覆盖限流器、缓存键、
二分收敛、多边形排序、IDW 插值、评分函数，外加「各接口坐标参数名与坐标顺序」常量表
的断言）。骨架任务不写 `test_*.py`，只提供包标记，使 `pytest tests/` 与
`python -m pytest` 在零测试文件时也能被收集到本目录。
"""
