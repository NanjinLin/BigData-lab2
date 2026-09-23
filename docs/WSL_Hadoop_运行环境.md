# 迭代一 Hadoop 运行环境

## 已验证的本机环境

- WSL 发行版：Ubuntu 24.04；Hadoop：3.5.0，安装在 `/opt/hadoop-3.5.0`；Java：OpenJDK 17，位于 `/usr/lib/jvm/java-17-openjdk-amd64`。
- Hadoop 配置源文件位于项目的 `hadoop-conf/`。WSL 中 `/home/linxinan/ml1m-hadoop-conf` 是指向该目录的符号链接；`/home/linxinan/ml1m-project` 指向本项目根目录，以避开路径中的空格。
- NameNode 与 DataNode 状态目录在 `/home/linxinan/.local/share/ml1m-hadoop/`，不在项目源码目录。NameNode 已完成首次格式化，**不能对现有目录再次执行 `hdfs namenode -format`**。
- 发行包从 Apache 镜像下载，并与 Apache 公布的 SHA-512 值 `04ab94496cc00c8b7a28d03f6308eff8d2a4e7f37a9da5e8e086e4d6fc990e7a94d661908f6a6136039536efb362614b8aecdef185b5fb8ed588f0b152c7aa16` 比对一致。

Apache Hadoop 3.5.0 服务端要求 Java 17；本机原有 Java 21 只适合其客户端使用，因此单独安装了 Java 17。官方说明见 <https://hadoop.apache.org/docs/r3.5.0/>。

## 启动与检查

本机 WSL 在没有持久会话时会自动关闭，守护进程也随之结束。运行项目期间，需保持一个 WSL 会话运行，例如在单独终端执行：

```powershell
wsl -d Ubuntu -- sleep infinity
```

另开终端启动四个单机服务。`systemd-run` 使服务脱离短暂命令会话；当前用户可使用免交互 `sudo -n`：

```bash
sudo -n systemd-run --unit=ml1m-namenode --property=User=linxinan --setenv=HADOOP_CONF_DIR=/home/linxinan/ml1m-hadoop-conf /opt/hadoop-3.5.0/bin/hdfs namenode
sudo -n systemd-run --unit=ml1m-datanode --property=User=linxinan --setenv=HADOOP_CONF_DIR=/home/linxinan/ml1m-hadoop-conf /opt/hadoop-3.5.0/bin/hdfs datanode
sudo -n systemd-run --unit=ml1m-resourcemanager --property=User=linxinan --setenv=HADOOP_CONF_DIR=/home/linxinan/ml1m-hadoop-conf /opt/hadoop-3.5.0/bin/yarn resourcemanager
sudo -n systemd-run --unit=ml1m-nodemanager --property=User=linxinan --setenv=HADOOP_CONF_DIR=/home/linxinan/ml1m-hadoop-conf /opt/hadoop-3.5.0/bin/yarn nodemanager
```

检查服务与节点：

```bash
systemctl is-active ml1m-namenode ml1m-datanode ml1m-resourcemanager ml1m-nodemanager
HADOOP_CONF_DIR=/home/linxinan/ml1m-hadoop-conf /opt/hadoop-3.5.0/bin/hdfs dfsadmin -report
HADOOP_CONF_DIR=/home/linxinan/ml1m-hadoop-conf /opt/hadoop-3.5.0/bin/yarn node -list
```

2026-09-23 的检查结果是四个服务均为 `active`、1 个存活 DataNode、1 个 `RUNNING` NodeManager。WSL 关闭后需重新启动服务，HDFS 持久化目录仍保留。

本次验证结束时已按 NodeManager、ResourceManager、DataNode、NameNode 的顺序停止服务，并关闭保持 WSL 运行的测试会话；再次开发时按本节重新启动即可。

## MapReduce 烟雾测试

将数据集自带 `README` 放入 HDFS 后，已提交 Apache 示例 `wordcount` 到 YARN：

- Job ID：`job_1790152466396_0001`
- 实际执行：1 个 map、1 个 reduce，均完成；作业状态为 `completed successfully`
- HDFS 输出：`/user/linxinan/ml1m/smoke/output/part-r-00000`

这是环境验证，不是项目清洗或正式质量评分。原始三个 `.dat` 文件尚未上传到 HDFS，也尚未产生 `T1/T2` 和五维分数。

开发中可使用 `sudo -n journalctl -u ml1m-namenode -n 50 --no-pager` 等命令检查服务日志。项目投入演示前，应由后端启动流程管理 WSL 会话和服务状态，用户提交清洗请求时不应接触上述命令。
