import xml.etree.ElementTree as ET

p = r"D:\work\项目\Enterprise Data Analyst Agent\_reg_skills_mcp.xml"
root = ET.parse(p).getroot()

# 根可能是 <testsuites>，属性在子节点 <testsuite>
tot = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
fails = []
for suite in (root if root.tag == "testsuites" else [root]):
    for k in tot:
        tot[k] += int(suite.attrib.get(k, 0) or 0)
    for tc in suite.iter("testcase"):
        for bad in list(tc.findall("failure")) + list(tc.findall("error")):
            fails.append((tc.attrib.get("classname", ""), tc.attrib.get("name", ""),
                          (bad.attrib.get("message") or "").strip()[:200]))

print("TOTAL:", tot)
print("FAIL LIST:", len(fails))
for cn, name, msg in fails:
    print(f"  - {cn}::{name}")
    print(f"      {msg}")
