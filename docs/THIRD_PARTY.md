# 上游依赖与复用边界

本项目新增业务代码采用 MIT。第三方依赖和模型仍各自适用原许可证，不能以本项目许可证替代。模型缓存、运行数据库和虚拟环境不随源码包分发。

## 直接复用

| 项目 | 用途 | 上游许可证及来源 |
| --- | --- | --- |
| pdfplumber | 数字 PDF 矢量表格单元格核对 | [MIT](https://github.com/jsvine/pdfplumber/blob/stable/LICENSE.txt) |
| Docling | PDF DOCX 转为结构化文档 | [MIT](https://github.com/docling-project/docling/blob/main/LICENSE) |
| Beautiful Soup | 静态 HTML DOM 提取 | [MIT](https://www.crummy.com/software/BeautifulSoup/) |
| lxml | HTML 解析后端 | [BSD 类许可及附带声明](https://github.com/lxml/lxml/blob/master/LICENSES.txt) |
| Pydantic | 数据与配置校验 | [MIT](https://github.com/pydantic/pydantic/blob/main/LICENSE) |
| PyYAML | 安全 YAML 读取 | [MIT](https://github.com/yaml/pyyaml/blob/main/LICENSE) |
| rank-bm25 | BM25 排序 | [Apache 2.0](https://github.com/dorianbrown/rank_bm25/blob/master/LICENSE) |
| Streamlit | 本地审阅界面 | [Apache 2.0](https://github.com/streamlit/streamlit/blob/develop/LICENSE) |

完整传递依赖与精确版本见 `uv.lock`；打包安装依赖时应保留其原声明。此表是关键直接依赖清单，并非全部传递依赖的法律审计。

## 实测模型

- [Docling layout heron](https://huggingface.co/docling-project/docling-layout-heron/blob/main/README.md)：模型卡标为 Apache 2.0。
- [Docling models 与 TableFormer](https://huggingface.co/docling-project/docling-models/blob/main/README.md)：模型卡列出 CDLA Permissive 2.0 与 Apache 2.0；以实际权重附带许可为准。
- [RapidOCR](https://github.com/RapidAI/RapidOCR)：通过 Docling 调用 ONNX Runtime 后端。OCR 模型资源按其上游来源与附带许可使用，不并入本项目 MIT。

本机验证使用的 Hub 模型 revision 和包版本记录于 `model-versions.json`。这些记录用于复现，不把模型文件重新发布至本仓库。

## 参考设计

[Data-Juicer](https://github.com/datajuicer/data-juicer) 的 YAML 顺序算子思想用于架构参考；本项目没有复制其源文件，也没有将其作为运行依赖。MarkItDown、Unstructured、Presidio 不在首版运行依赖中。

## 自制样例

`examples/` 中所有 HTML、JSON、DOCX、PDF 由本项目脚本创建，按本项目 MIT 许可提供。品牌 Aurora 和 Atlas、参数、部件号和联系方式均为演示用途，不构成维修依据。没有附带厂商受限手册或真实客户资料。
