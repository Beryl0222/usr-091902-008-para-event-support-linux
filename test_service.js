"use strict";

const { spawnSync } = require("node:child_process");

// 先跑新增的领域/运行时/接口测试，再跑保留的服务契约测试。
const steps = [
  ["python3", ["-m", "unittest", "discover", "-v", "-p", "test_*.py"]],
  ["python3", ["-m", "unittest", "-v", "service_contract"]],
];

for (const [cmd, args] of steps) {
  const result = spawnSync(cmd, args, { stdio: "inherit" });
  if (result.error) {
    console.error(result.error.message);
    process.exit(1);
  }
  if ((result.status ?? 1) !== 0) process.exit(result.status ?? 1);
}
