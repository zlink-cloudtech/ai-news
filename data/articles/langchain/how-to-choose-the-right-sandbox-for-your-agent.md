<!-- source: langchain | url: https://www.langchain.com/blog/how-to-choose-the-right-sandbox-for-your-agent | fetched_at: 2026-06-13T16:16:35+08:00 -->

AI agents are most useful when they can take action, and letting them write and run code is one of the highest-value capabilities you can give them. But that autonomy comes with security risks. Agent-written code can create unexpected threats to your data and systems, so you need to control where that code runs and what it can access.
Sandboxes give teams that control. They isolate AI-generated code, limit its permissions, and create a safer boundary for letting agents do real work. They’re computers your agent can safely use. This guide covers the security risks of running AI-generated code and how to choose a sandboxing approach that protects your data and systems without limiting agent usefulness.
The Agent Lethal Trifecta
LLMs excel at software engineering, making code generation and execution one of the best ways for agents to create value. The code itself can be the output, or the agent can create programs to help it accomplish non-software engineering tasks. A common example of the latter is writing one-off scripts to help with custom data analysis. The risk, however, is that an attacker can use prompt injection to induce your agent to write code that compromises your data and systems.
Currently, there is no surefire way to prevent prompt injection, and untrusted content containing a prompt injection attack can reach your agent’s context in many ways. End-user inputs, external MCP server responses, and third party-written skills are just a few common examples. Simon Willison, co-author of the popular Django Web Framework and inventor of the term “prompt injection”, summarizes the ensuing risk by outlining the “lethal trifecta” of conditions that, when all true, means your agent can allow an attacker to steal your data:
- Access to sensitive data
- Exposure to untrusted content
- The ability to communicate externally
Inspired by Willison’s work, Meta proposed applying the “Rule of Two”, meaning an agent should never run fully autonomously if all three conditions of the trifecta apply.
The rule of two is simple to understand, but following it is difficult. Providing an agent with the tools it needs usually means giving it both access to sensitive data and the ability to communicate externally. Advances in foundation models and harness engineering allow AI agents to leverage more and more context, but that increases the odds an attacker can send a prompt injection attack.
Odds are, the lethal trifecta applies to your agent, which means it needs safeguards limiting what actions it can take with what data. Successfully implementing those controls is a lot easier if you first narrow all three risk factors. That’s where sandboxes help.
Using sandboxes to put clear boundaries around what your agent can do
A secure sandbox solution provides the following features to narrow an agent’s exposure to the lethal trifecta, and limit the harm it can do in the event of a successful prompt injection attack:
- Isolated filesystem
- Limited network access
- Resource limits
- Controlled reusability
- Kernel-level isolation from the host machine
There are products on the market calling themselves "sandboxes" that don't provide this functionality out of the box, so evaluate potential solutions critically before choosing one for your agent. For example, the open-source Kubernetes Agent Sandbox is only secure if deployed to a cluster that has kernel-level isolation between containers, which most Kubernetes clusters do not enforce.
Isolated Filesystem
The sandbox should contain only the data the agent needs to do its work, and block the agent from trying to access any other data. This minimizes exposure to sensitive data while making it easier to audit and control.
Limited network access
The sandbox should let you specify which external endpoints the agent can send data to via the internet. This means that, even if an attacker induces your agent to leak sensitive data, it can only send it to entities you trust.
Resource limits
The sandbox should let you control how much compute and memory the agent can use and for how long. A compromised agent should not be able to overconsume your system’s resources.
Controlled reusability
Reusing sandboxes can be a convenient way to persist agent state across executions, but it does mean that if an attacker compromises the sandbox once, the damage can persist. The sandbox solution should let you decide if that risk is worth taking.
Kernel-level isolation from the host machine
The controls above only work if the agent can’t override the systems enforcing them. Unfortunately, the kernels that power operating systems often contain bugs a compromised agent can exploit to take control of your machine and bypass any controls protecting the data on it. The solution here is virtualization, meaning your sandbox runs using its own kernel separate from the one powering the machine running it. You can use a microVM-powered solution to efficiently create kernel-isolated sandboxes without incurring the costs of spinning up a full virtual machine per sandbox.
A sandbox solution that has all of the above doesn’t mean you don’t need to think seriously about the risks a compromised agent poses to your data and systems. Sandboxes alone do not fully eliminate any aspect of the lethal trifecta. Instead, they shrink access to sensitive data and limit external communication to the point where managing prompt injection risk becomes a small enough problem for your team to solve confidently.
LangSmith Sandboxes: secure code execution, integrated with our end-to-end agent engineering platform
We developed the LangSmith agent engineering platform to complement our open source products, letting you take the agents you built with LangChain, LangGraph, and/or Deep Agents and robustly test, deploy, and monitor them. Since sandboxes are often integral to building secure agents, we built a managed solution into LangSmith. We designed the product following the best practices listed above and subjected it to rigorous penetration testing. LangSmith Sandboxes are in use by companies of all sizes, including enterprises like Monday.com
LangSmith Sandboxes are each backed by a dedicated microVM with its own filesystem, giving them kernel-level isolation from each other as well as the underlying infrastructure. You control their lifecycle from start to shutdown to eventual destruction, reusing them as much or as little as you need along the way. You decide what network access, if any, a process running inside the sandbox gets. We also include an authorization proxy that injects secure credentials into outbound traffic after it leaves the sandbox, so you don’t need to put secrets inside the sandbox, where an untrusted process could access and try to misuse them.
You can manage LangSmith Sandboxes using the same API and authentication you already use with our observability, evaluations, and deployment functionality. Adding sandbox use to an agent built with LangChain, LangGraph, or Deep Agents takes just a few lines of code.
If you are ready to use sandboxes to keep your agents both useful and secure, you can get started with LangSmith Sandboxes today following the documentation here.
