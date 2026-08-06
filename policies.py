"""The 10 IT policies for Helix Industries — the only authorized knowledge source.

Structured so retriever.py can index at section granularity and agent.py can
cite a stable {policy_id, section} pair. Do not let the agent answer or act
from anything not present here (see ground_check in agent.py).
"""

POLICIES = {
    "POL-01": {
        "title": "Password & Authentication Policy",
        "effective": "2025-09-01",
        "owner": "Identity & Access Management team",
        "sections": {
            "1.1": "Standard user passwords must be at least 14 characters, contain three of four character classes, and not match any of the previous 12 passwords.",
            "1.2": "Standard accounts rotate passwords annually. Privileged accounts (domain admins, root, DBA) rotate every 90 days.",
            "1.3": "Multi-factor authentication (MFA) is mandatory for every corporate application and is enforced via Okta. Acceptable second factors: Okta Verify push, FIDO2 security key, or TOTP.",
            "1.4": "Accounts are locked after 5 consecutive failed login attempts. Self-service unlock is available after 15 minutes via the password portal; otherwise contact the Service Desk.",
            "1.5": "1Password Enterprise is the sanctioned password manager. Storing corporate credentials in browsers, sticky notes, or personal password managers is prohibited.",
            "1.6": "Privileged users must additionally authenticate with a YubiKey 5 series hardware token. Soft tokens alone do not satisfy privileged access requirements.",
        },
    },
    "POL-02": {
        "title": "VPN & Remote Access Policy",
        "effective": "2025-07-15",
        "owner": "Network Security",
        "sections": {
            "2.1": "Cisco AnyConnect is the only approved VPN client. Personal VPNs (NordVPN, ExpressVPN, etc.) must not be installed on corporate endpoints.",
            "2.2": "Split tunneling is disabled by policy. All traffic is routed through the corporate gateway and inspected by Zscaler.",
            "2.3": "VPN sessions terminate after 12 hours of connection or 30 minutes of inactivity, whichever comes first.",
            "2.4": "Public or untrusted Wi-Fi (hotels, cafes, airports) is permitted only when AnyConnect is active before any other traffic.",
            "2.5": "Access is geo-restricted to the Approved Country List maintained by Network Security. Connecting from outside the list requires a Travel Exception ticket submitted at least 5 business days in advance. (Approved list includes US-East and EU-Central / Germany; Japan and Vietnam are not on the list.)",
            "2.6": "Privileged remote access to production systems is brokered through CyberArk PAM. Direct SSH/RDP to production from a laptop is forbidden.",
        },
    },
    "POL-03": {
        "title": "Acceptable Use Policy",
        "effective": "2025-01-01",
        "owner": "Information Security",
        "sections": {
            "3.1": "Corporate devices are issued primarily for business use. Incidental personal use is allowed when it does not interfere with work or violate any other policy.",
            "3.2": "Prohibited activities: peer-to-peer file sharing, gambling, adult content, cryptocurrency mining, and any unlicensed streaming.",
            "3.3": "Web traffic is filtered and logged via Zscaler. Logs are retained for 12 months and reviewed only on lawful basis.",
            "3.4": "USB mass storage is blocked by default. Exceptions for business need can be requested via the 'USB Exception' form in ServiceNow with manager approval.",
            "3.5": "Personal cloud storage (Dropbox personal, iCloud Drive, Google Drive consumer) is blocked. Use the corporate Box tenant or OneDrive for Business instead.",
            "3.6": "Corporate devices must remain under the control of the assigned employee. Lending the device to family members or external parties is prohibited.",
        },
    },
    "POL-04": {
        "title": "Software Installation & Procurement Policy",
        "effective": "2025-03-10",
        "owner": "IT Procurement",
        "sections": {
            "4.1": "Only software listed in the Approved Software Catalog (ServiceNow > Software Center) may be installed without a ticket. End users can self-serve catalog apps.",
            "4.2": "New software requests follow a 5-business-day SLA. Reviews include InfoSec (data classification, telemetry), Procurement (licensing), and Legal (terms of service).",
            "4.3": "Open-source libraries used in internal tools require a Software Bill of Materials (SBOM) and a license review against the Approved License List. GPL and AGPL components require special exception.",
            "4.4": "Corporate email addresses must not be used to sign up for unapproved SaaS free trials or personal accounts.",
            "4.5": "Browser extensions are restricted to the Allowed Extensions List enforced via Chrome and Edge management. Other extensions are blocked at install time.",
            "4.6": "Local admin rights are removed by default. Time-bound admin elevation can be requested through Make-Me-Admin for a maximum of 60 minutes per session. Permanent local admin is not self-service and requires an Endpoint Engineering exception.",
        },
    },
    "POL-05": {
        "title": "Data Classification & Handling Policy",
        "effective": "2025-04-01",
        "owner": "Data Governance",
        "sections": {
            "5.1": "Helix data is classified into four tiers: Public, Internal, Confidential, and Restricted. Every document inherits the highest tier of any field it contains.",
            "5.2": "Restricted data (PHI, payment card data, source code for revenue-critical systems) must be encrypted both at rest and in transit, and may only reside in approved geographies (US-East, EU-Central).",
            "5.3": "Confidential data may not be sent to external recipients without a Data Loss Prevention (DLP) exception. The DLP exception process requires data owner approval and is valid for 30 days.",
            "5.4": "EU personal data is subject to GDPR controls; transfer outside the EEA requires Standard Contractual Clauses on file.",
            "5.5": "Retention follows the published Records Retention Schedule. Default for unclassified business records is 7 years; PHI is 10 years; payment data is purged at 13 months.",
            "5.6": "Auto-forwarding corporate email to any external address (including personal Gmail) is technically blocked and policy-prohibited.",
        },
    },
    "POL-06": {
        "title": "BYOD (Bring Your Own Device) Policy",
        "effective": "2025-02-01",
        "owner": "Endpoint Engineering",
        "sections": {
            "6.1": "Personal devices are permitted for corporate email, calendar, and Teams only. They must be enrolled in Microsoft Intune for MDM management.",
            "6.2": "Enrollment establishes a managed work container. IT can remote-wipe only the corporate container; personal data is not touched.",
            "6.3": "Restricted and Confidential data must never be stored on a BYOD device outside the managed container.",
            "6.4": "Jailbroken or rooted devices are blocked from enrollment and from accessing corporate resources.",
            "6.5": "Operating systems must be within two major versions of current vendor releases. Older OS versions lose access automatically.",
            "6.6": "A $50/month BYOD stipend is available for roles flagged 'mobile-eligible' in Workday. Employees on the stipend forfeit eligibility for a corporate-issued mobile phone.",
        },
    },
    "POL-07": {
        "title": "Email & Communication Security Policy",
        "effective": "2025-06-01",
        "owner": "Messaging Security",
        "sections": {
            "7.1": "All outbound email is sent over TLS. Recipients whose servers do not support TLS receive a portal-pickup link instead of plaintext mail.",
            "7.2": "Suspicious emails should be reported using the Phish Alert Button in Outlook. Do not forward suspicious emails manually.",
            "7.3": "Every email from an external sender is prefixed with an [EXTERNAL] banner. Treat any 'CEO request' from an [EXTERNAL] address as suspect.",
            "7.4": "Attachments larger than 25 MB are blocked at the gateway. Use Box or OneDrive sharing links for large files.",
            "7.5": "Phishing simulations run monthly. Employees who fail two simulations within 12 months are auto-enrolled in additional training.",
            "7.6": "Auto-forwarding rules to external addresses are blocked at the mailbox level and cannot be created by end users.",
        },
    },
    "POL-08": {
        "title": "Hardware Request & Asset Management Policy",
        "effective": "2025-05-01",
        "owner": "IT Asset Management",
        "sections": {
            "8.1": "The standard laptop refresh cycle is 36 months. Eligibility is calculated from the asset's first-issue date in the CMDB.",
            "8.2": "New-hire hardware requests must be submitted by the hiring manager at least 10 business days before the employee's start date.",
            "8.3": "Lost or stolen devices must be reported within 24 hours via the 'Lost/Stolen Device' ticket. A police report is required if the device was stolen; the case number must be attached to the ticket.",
            "8.4": "Repairs are performed only at the IT Depot in Austin or by approved third-party vendors (Apple Business Repair, Dell ProSupport). Third-party walk-in repairs are not reimbursable.",
            "8.5": "On offboarding, IT mails a prepaid return kit to the employee's address on file. Devices must be shipped within 5 business days of the last working day.",
            "8.6": "Peripherals (monitor, keyboard, mouse, dock) follow a 5-year refresh and are requested through the Peripheral Catalog.",
        },
    },
    "POL-09": {
        "title": "Security Incident Reporting Policy",
        "effective": "2025-08-01",
        "owner": "Security Operations Center (SOC)",
        "sections": {
            "9.1": "Suspected security incidents must be reported within 1 hour of discovery to security@helix.example or via the 24/7 SOC hotline at extension 4357 (HELP).",
            "9.2": "If you suspect a compromise, do NOT power off the device. Disconnect it from the network (unplug Ethernet, disable Wi-Fi) and wait for SOC instructions to preserve forensic evidence.",
            "9.3": "Severity tiers: SEV-1 (active breach, customer data at risk), SEV-2 (probable breach or major outage), SEV-3 (contained incident), SEV-4 (informational).",
            "9.4": "An Incident Commander is assigned for SEV-1 and SEV-2 incidents. The IC owns external communication; individual employees must not speak to press or customers about the incident.",
            "9.5": "Tabletop exercises are mandatory quarterly for all employees in roles tagged 'incident-responder' in Workday.",
            "9.6": "Lost or stolen devices that are confirmed to contain Restricted data are automatically escalated to a SEV-2 incident.",
        },
    },
    "POL-10": {
        "title": "Access Provisioning & Deprovisioning Policy",
        "effective": "2025-01-15",
        "owner": "Identity & Access Management",
        "sections": {
            "10.1": "New hires receive access based on the role-based access (RBAC) template attached to their Workday job code at the time HR marks them 'active'.",
            "10.2": "Any access beyond the default RBAC template requires manager approval plus, for Restricted-tier systems, data owner approval.",
            "10.3": "Access reviews are run quarterly. Reviewers have 14 calendar days to certify or revoke entitlements; un-certified access is automatically revoked.",
            "10.4": "On termination or resignation, all access is revoked within 1 hour of HR triggering the 'separation' event in Workday.",
            "10.5": "Contractor accounts have a maximum 6-month duration and require manager renewal. Unrenewed accounts disable automatically.",
            "10.6": "Shared accounts are prohibited. Exceptions are limited to service accounts owned by a named engineering team with rotating credentials in CyberArk.",
        },
    },
}


def get_section(policy_id: str, section: str) -> str | None:
    policy = POLICIES.get(policy_id)
    if not policy:
        return None
    return policy["sections"].get(section)


def all_sections():
    """Yield (policy_id, section_id, title, text) for every section — the retrieval corpus."""
    for policy_id, policy in POLICIES.items():
        for section_id, text in policy["sections"].items():
            yield policy_id, section_id, policy["title"], text
