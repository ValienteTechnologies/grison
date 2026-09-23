# Fixture corpus attribution

Real scanner-export samples vendored into `tests/fixtures/scanners/<scanner>/` for `tests/scanners/test_golden.py` and `tests/scanners/test_contract.py`. Every file below is copied verbatim (byte-for-byte) from the source path/commit listed, prefixed with its origin (`dojo-` or `reptor-`) so provenance is visible from the filename alone. Hand-made synthetic fixtures (unprefixed names) are original to this repo and carry no third-party license.

- DefectDojo samples: `https://github.com/DefectDojo/django-DefectDojo`, commit `706b9d5235c91dd842a712770b1b16e218077981`, BSD-3-Clause (Copyright (c) 2015-2023 DefectDojo, Inc. All rights reserved.)
- reptor samples: `https://github.com/Syslifters/reptor/`, commit `0e5899f2ce17ceb6a4ef928a611ee977d2069411`, MIT (Copyright (c) 2023 SysReptor+reptor Developers)

## Vendored files

| file | source repo | source path | commit | license |
|---|---|---|---|---|
| `acunetix/dojo-XML_http_example_co_id_.xml` | https://github.com/DefectDojo/django-DefectDojo | `unittests/scans/acunetix/XML_http_example_co_id_.xml` | `706b9d5235c91dd842a712770b1b16e218077981` | BSD-3-Clause |
| `acunetix/dojo-many_findings.xml` | https://github.com/DefectDojo/django-DefectDojo | `unittests/scans/acunetix/many_findings.xml` | `706b9d5235c91dd842a712770b1b16e218077981` | BSD-3-Clause |
| `acunetix/dojo-one_finding.xml` | https://github.com/DefectDojo/django-DefectDojo | `unittests/scans/acunetix/one_finding.xml` | `706b9d5235c91dd842a712770b1b16e218077981` | BSD-3-Clause |
| `acunetix/dojo-many_findings_with_port_number.xml` | https://github.com/DefectDojo/django-DefectDojo | `unittests/scans/acunetix/many_findings_with_port_number.xml` | `706b9d5235c91dd842a712770b1b16e218077981` | BSD-3-Clause |
| `acunetix/dojo-watson_test_unique.xml` | https://github.com/DefectDojo/django-DefectDojo | `unittests/scans/acunetix/watson_test_unique.xml` | `706b9d5235c91dd842a712770b1b16e218077981` | BSD-3-Clause |
| `burp/dojo-one_finding.xml` | https://github.com/DefectDojo/django-DefectDojo | `unittests/scans/burp/one_finding.xml` | `706b9d5235c91dd842a712770b1b16e218077981` | BSD-3-Clause |
| `burp/dojo-one_finding_with_blank_response.xml` | https://github.com/DefectDojo/django-DefectDojo | `unittests/scans/burp/one_finding_with_blank_response.xml` | `706b9d5235c91dd842a712770b1b16e218077981` | BSD-3-Clause |
| `burp/dojo-one_finding_with_cwe.xml` | https://github.com/DefectDojo/django-DefectDojo | `unittests/scans/burp/one_finding_with_cwe.xml` | `706b9d5235c91dd842a712770b1b16e218077981` | BSD-3-Clause |
| `burp/dojo-seven_findings.xml` | https://github.com/DefectDojo/django-DefectDojo | `unittests/scans/burp/seven_findings.xml` | `706b9d5235c91dd842a712770b1b16e218077981` | BSD-3-Clause |
| `nessus/dojo-nessus_many_vuln.xml` | https://github.com/DefectDojo/django-DefectDojo | `unittests/scans/tenable/nessus/nessus_many_vuln.xml` | `706b9d5235c91dd842a712770b1b16e218077981` | BSD-3-Clause |
| `nessus/dojo-nessus_v_unknown.xml` | https://github.com/DefectDojo/django-DefectDojo | `unittests/scans/tenable/nessus/nessus_v_unknown.xml` | `706b9d5235c91dd842a712770b1b16e218077981` | BSD-3-Clause |
| `nessus/dojo-nessus_was_many_vuln.xml` | https://github.com/DefectDojo/django-DefectDojo | `unittests/scans/tenable/nessus_was/nessus_was_many_vuln.xml` | `706b9d5235c91dd842a712770b1b16e218077981` | BSD-3-Clause |
| `nessus/dojo-nessus_was_no_vuln.xml` | https://github.com/DefectDojo/django-DefectDojo | `unittests/scans/tenable/nessus_was/nessus_was_no_vuln.xml` | `706b9d5235c91dd842a712770b1b16e218077981` | BSD-3-Clause |
| `nessus/dojo-nessus_was_one_vuln.xml` | https://github.com/DefectDojo/django-DefectDojo | `unittests/scans/tenable/nessus_was/nessus_was_one_vuln.xml` | `706b9d5235c91dd842a712770b1b16e218077981` | BSD-3-Clause |
| `nessus/reptor-nessus_multi_host.xml` | https://github.com/Syslifters/reptor/ | `reptor/plugins/tools/Nessus/tests/data/nessus_multi_host.xml` | `0e5899f2ce17ceb6a4ef928a611ee977d2069411` | MIT |
| `nessus/reptor-nessus_single_host.xml` | https://github.com/Syslifters/reptor/ | `reptor/plugins/tools/Nessus/tests/data/nessus_single_host.xml` | `0e5899f2ce17ceb6a4ef928a611ee977d2069411` | MIT |
| `nessus/reptor-nessus_v_unknown.nessus` | https://github.com/Syslifters/reptor/ | `reptor/plugins/tools/Nessus/tests/data/nessus_v_unknown.nessus` | `0e5899f2ce17ceb6a4ef928a611ee977d2069411` | MIT |
| `nessus/reptor-test.nessus` | https://github.com/Syslifters/reptor/ | `reptor/plugins/tools/Nessus/tests/data/test.nessus` | `0e5899f2ce17ceb6a4ef928a611ee977d2069411` | MIT |
| `nmap/dojo-issue4406.xml` | https://github.com/DefectDojo/django-DefectDojo | `unittests/scans/nmap/issue4406.xml` | `706b9d5235c91dd842a712770b1b16e218077981` | BSD-3-Clause |
| `nmap/dojo-nmap_0port.xml` | https://github.com/DefectDojo/django-DefectDojo | `unittests/scans/nmap/nmap_0port.xml` | `706b9d5235c91dd842a712770b1b16e218077981` | BSD-3-Clause |
| `nmap/dojo-nmap_1port.xml` | https://github.com/DefectDojo/django-DefectDojo | `unittests/scans/nmap/nmap_1port.xml` | `706b9d5235c91dd842a712770b1b16e218077981` | BSD-3-Clause |
| `nmap/dojo-nmap_multiple_port.xml` | https://github.com/DefectDojo/django-DefectDojo | `unittests/scans/nmap/nmap_multiple_port.xml` | `706b9d5235c91dd842a712770b1b16e218077981` | BSD-3-Clause |
| `nmap/dojo-nmap_script_vulners.xml` | https://github.com/DefectDojo/django-DefectDojo | `unittests/scans/nmap/nmap_script_vulners.xml` | `706b9d5235c91dd842a712770b1b16e218077981` | BSD-3-Clause |
| `nmap/dojo-issue12411.xml` | https://github.com/DefectDojo/django-DefectDojo | `unittests/scans/nmap/issue12411.xml` | `706b9d5235c91dd842a712770b1b16e218077981` | BSD-3-Clause |
| `nmap/reptor-nmap_multi_target.xml` | https://github.com/Syslifters/reptor/ | `reptor/plugins/tools/Nmap/tests/data/nmap_multi_target.xml` | `0e5899f2ce17ceb6a4ef928a611ee977d2069411` | MIT |
| `nmap/reptor-nmap_with_mac.xml` | https://github.com/Syslifters/reptor/ | `reptor/plugins/tools/Nmap/tests/data/nmap_with_mac.xml` | `0e5899f2ce17ceb6a4ef928a611ee977d2069411` | MIT |
| `openvas/dojo-many_vuln.xml` | https://github.com/DefectDojo/django-DefectDojo | `unittests/scans/openvas/many_vuln.xml` | `706b9d5235c91dd842a712770b1b16e218077981` | BSD-3-Clause |
| `openvas/dojo-no_vuln.xml` | https://github.com/DefectDojo/django-DefectDojo | `unittests/scans/openvas/no_vuln.xml` | `706b9d5235c91dd842a712770b1b16e218077981` | BSD-3-Clause |
| `openvas/dojo-one_vuln.xml` | https://github.com/DefectDojo/django-DefectDojo | `unittests/scans/openvas/one_vuln.xml` | `706b9d5235c91dd842a712770b1b16e218077981` | BSD-3-Clause |
| `openvas/dojo-report_detail_v2.xml` | https://github.com/DefectDojo/django-DefectDojo | `unittests/scans/openvas/report_detail_v2.xml` | `706b9d5235c91dd842a712770b1b16e218077981` | BSD-3-Clause |
| `openvas/reptor-openvas.xml` | https://github.com/Syslifters/reptor/ | `reptor/plugins/tools/OpenVAS/tests/data/openvas.xml` | `0e5899f2ce17ceb6a4ef928a611ee977d2069411` | MIT |
| `qualys/dojo-Qualys_Sample_Report.xml` | https://github.com/DefectDojo/django-DefectDojo | `unittests/scans/qualys/Qualys_Sample_Report.xml` | `706b9d5235c91dd842a712770b1b16e218077981` | BSD-3-Clause |
| `qualys/dojo-empty.xml` | https://github.com/DefectDojo/django-DefectDojo | `unittests/scans/qualys/empty.xml` | `706b9d5235c91dd842a712770b1b16e218077981` | BSD-3-Clause |
| `qualys/dojo-discussion_10239.xml` | https://github.com/DefectDojo/django-DefectDojo | `unittests/scans/qualys_webapp/discussion_10239.xml` | `706b9d5235c91dd842a712770b1b16e218077981` | BSD-3-Clause |
| `qualys/dojo-qualys_webapp_many_vuln.xml` | https://github.com/DefectDojo/django-DefectDojo | `unittests/scans/qualys_webapp/qualys_webapp_many_vuln.xml` | `706b9d5235c91dd842a712770b1b16e218077981` | BSD-3-Clause |
| `qualys/dojo-qualys_webapp_no_vuln.xml` | https://github.com/DefectDojo/django-DefectDojo | `unittests/scans/qualys_webapp/qualys_webapp_no_vuln.xml` | `706b9d5235c91dd842a712770b1b16e218077981` | BSD-3-Clause |
| `qualys/dojo-qualys_webapp_one_vuln.xml` | https://github.com/DefectDojo/django-DefectDojo | `unittests/scans/qualys_webapp/qualys_webapp_one_vuln.xml` | `706b9d5235c91dd842a712770b1b16e218077981` | BSD-3-Clause |
| `qualys/reptor-vuln_scan.xml` | https://github.com/Syslifters/reptor/ | `reptor/plugins/tools/Qualys/tests/data/vuln_scan.xml` | `0e5899f2ce17ceb6a4ef928a611ee977d2069411` | MIT |
| `qualys/reptor-webapp_scan.xml` | https://github.com/Syslifters/reptor/ | `reptor/plugins/tools/Qualys/tests/data/webapp_scan.xml` | `0e5899f2ce17ceb6a4ef928a611ee977d2069411` | MIT |
| `sslyze/dojo-issue_9848.json` | https://github.com/DefectDojo/django-DefectDojo | `unittests/scans/sslyze/issue_9848.json` | `706b9d5235c91dd842a712770b1b16e218077981` | BSD-3-Clause |
| `sslyze/dojo-one_target_many_vuln_new.json` | https://github.com/DefectDojo/django-DefectDojo | `unittests/scans/sslyze/one_target_many_vuln_new.json` | `706b9d5235c91dd842a712770b1b16e218077981` | BSD-3-Clause |
| `sslyze/dojo-one_target_many_vuln_old.json` | https://github.com/DefectDojo/django-DefectDojo | `unittests/scans/sslyze/one_target_many_vuln_old.json` | `706b9d5235c91dd842a712770b1b16e218077981` | BSD-3-Clause |
| `sslyze/dojo-one_target_one_vuln_new.json` | https://github.com/DefectDojo/django-DefectDojo | `unittests/scans/sslyze/one_target_one_vuln_new.json` | `706b9d5235c91dd842a712770b1b16e218077981` | BSD-3-Clause |
| `sslyze/dojo-one_target_zero_vuln_new.json` | https://github.com/DefectDojo/django-DefectDojo | `unittests/scans/sslyze/one_target_zero_vuln_new.json` | `706b9d5235c91dd842a712770b1b16e218077981` | BSD-3-Clause |
| `sslyze/dojo-two_targets_many_vuln_new.json` | https://github.com/DefectDojo/django-DefectDojo | `unittests/scans/sslyze/two_targets_many_vuln_new.json` | `706b9d5235c91dd842a712770b1b16e218077981` | BSD-3-Clause |
| `sslyze/dojo-two_targets_two_vuln_old.json` | https://github.com/DefectDojo/django-DefectDojo | `unittests/scans/sslyze/two_targets_two_vuln_old.json` | `706b9d5235c91dd842a712770b1b16e218077981` | BSD-3-Clause |
| `sslyze/reptor-sslyze_v5.json` | https://github.com/Syslifters/reptor/ | `reptor/plugins/tools/Sslyze/tests/data/sslyze_v5.json` | `0e5899f2ce17ceb6a4ef928a611ee977d2069411` | MIT |
| `zap/dojo-0_zap_sample.xml` | https://github.com/DefectDojo/django-DefectDojo | `unittests/scans/zap/0_zap_sample.xml` | `706b9d5235c91dd842a712770b1b16e218077981` | BSD-3-Clause |
| `zap/dojo-0_zap_sample_without_zap3.xml` | https://github.com/DefectDojo/django-DefectDojo | `unittests/scans/zap/0_zap_sample_without_zap3.xml` | `706b9d5235c91dd842a712770b1b16e218077981` | BSD-3-Clause |
| `zap/dojo-1_zap_sample_0_and_new_absent.xml` | https://github.com/DefectDojo/django-DefectDojo | `unittests/scans/zap/1_zap_sample_0_and_new_absent.xml` | `706b9d5235c91dd842a712770b1b16e218077981` | BSD-3-Clause |
| `zap/dojo-empty_2.9.0.xml` | https://github.com/DefectDojo/django-DefectDojo | `unittests/scans/zap/empty_2.9.0.xml` | `706b9d5235c91dd842a712770b1b16e218077981` | BSD-3-Clause |
| `zap/dojo-juicy2.xml` | https://github.com/DefectDojo/django-DefectDojo | `unittests/scans/zap/juicy2.xml` | `706b9d5235c91dd842a712770b1b16e218077981` | BSD-3-Clause |
| `zap/dojo-zap-results-first-scan.xml` | https://github.com/DefectDojo/django-DefectDojo | `unittests/scans/zap/zap-results-first-scan.xml` | `706b9d5235c91dd842a712770b1b16e218077981` | BSD-3-Clause |
| `zap/dojo-zap-xml-plus-format.xml` | https://github.com/DefectDojo/django-DefectDojo | `unittests/scans/zap/zap-xml-plus-format.xml` | `706b9d5235c91dd842a712770b1b16e218077981` | BSD-3-Clause |
| `zap/dojo-zap_2.16.1_with_req_resp.xml` | https://github.com/DefectDojo/django-DefectDojo | `unittests/scans/zap/zap_2.16.1_with_req_resp.xml` | `706b9d5235c91dd842a712770b1b16e218077981` | BSD-3-Clause |
| `zap/reptor-zap-report-NoReqRes.xml` | https://github.com/Syslifters/reptor/ | `reptor/plugins/tools/Zap/tests/data/zap-report-NoReqRes.xml` | `0e5899f2ce17ceb6a4ef928a611ee977d2069411` | MIT |
| `zap/reptor-zap-report.json` | https://github.com/Syslifters/reptor/ | `reptor/plugins/tools/Zap/tests/data/zap-report.json` | `0e5899f2ce17ceb6a4ef928a611ee977d2069411` | MIT |

## Hand-made fixtures (no third-party source)

| file |
|---|
| `acunetix/acunetix_sample.xml` |
| `burp/burp_sample.xml` |
| `nessus/nessus_sample.xml` |
| `nmap/nmap_sample.xml` |
| `openvas/openvas_sample.xml` |
| `qualys/qualys_sample.xml` |
| `qualys/qualys_was_sample.xml` |
| `sslyze/sslyze_sample.json` |
| `zap/zap_sample.xml` |

## License texts

### BSD-3-Clause (DefectDojo)

```
### Copyright (c) 2015-2023 DefectDojo, Inc. All rights reserved.

Redistribution and use in source and binary forms, with or without modification, are permitted provided that the following conditions are met:

1. Redistributions of source code must retain the above copyright notice, this list of conditions and the following disclaimer.

2. Redistributions in binary form must reproduce the above copyright notice, this list of conditions and the following disclaimer in the documentation and/or other materials provided with the distribution.

3. Neither the name of the copyright holder nor the names of its contributors may be used to endorse or promote products derived from this software without specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
```

### MIT (reptor)

```
Copyright (c) 2023 SysReptor+reptor Developers
See also the CREDITS.md who contributed to this project.

Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated documentation files (the “Software”), to deal in the Software without restriction, including without limitation the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED “AS IS”, WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
```
