"""User-supplied A-section rules. These are business requirements, not market facts."""
def rule(model,label,aliases,forms,reference,threshold,notes=''):
    return {'model':model,'label':label,'aliases':aliases,'resource_forms':forms,'reference_discount':reference,'min_ratio':0,'max_ratio':threshold,'review_margin':0,'currency':'USD','unit':'1M tokens','reference_input':None,'note':notes,'source':'用户提供的 A 部分报价规则'}

A_RULES=[
    rule('kimi-k3','Kimi K3',['Kimi K3','K3','kimi-k3'],['自部署'],.45,.45),
    rule('kimi-k3.1','Kimi K3.1',['Kimi K3.1','K3.1','kimi-k3.1','kimi-k3.1-pro','kimi-k3.1-pro-highspeed'],['官转'],.65,.65,'包含 pro、pro-highspeed'),
    rule('glm-5.3','GLM-5.3（含 Flash）',['GLM-5.3','GLM5.3','glm-5.3-flash'],['自部署','官转'],[.4,.5],.4,'正式 A 文本及配置明确：≤4折可谈，覆盖截图空白'),
    rule('glm-5.2','GLM-5.2',['GLM-5.2','GLM5.2'],['自部署','官转'],.5,.4),
    rule('glm-5.1','GLM-5.1',['GLM-5.1','GLM5.1'],['自部署','官转'],.65,.55),
    rule('deepseek-v4','DeepSeek V4',['DeepSeek V4','DeepSeek-V4','DS-V4','deepseek-v4-pro','deepseek-v4-flash'],['自部署','官转'],.4,.4,'包含 PRO、Flash 和日期后缀；不包含 V4.1'),
    rule('deepseek-v4.1','DeepSeek V4.1',['DeepSeek V4.1','DeepSeek-V4.1','DS-V4.1','deepseek-v4.1-flash'],[],.45,.45,'包含 Flash 和日期后缀；资源形态未区分'),
    rule('qwen','Qwen 系列',['Qwen','Qwen3.8-Max','Qwen3.8-Max (Batch File)','Qwen3.8-Max (Batch Chat)','Qwen3.7-Max'],[],.5,.5,'包含用户列出的 Batch File / Batch Chat；资源形态未区分'),
    rule('seedance','Seedance 系列',['Seedance','Doubao-Seedance','SD'],[],.78,.78,'SD 为歧义别名，必须结合页面上下文确认，不能匹配 Stable Diffusion'),
    rule('wan3.0','Wan3.0 系列',['Wan3.0','Wan-3.0','VideoPrime'],['官key'],.68,.68,'含 VideoPrime'),
]

# User confirmed after source reconciliation: GLM 5.2/5.1 thresholds follow resource form.
for item in A_RULES:
    if item['model'] in ['glm-5.2','glm-5.1']:
        item.update(max_ratio=None,reference_discount=None,
                    resource_thresholds={'自部署':.4,'官转':.55},
                    reference_by_resource={'自部署':.5,'官转':.65},
                    note='用户确认：按资源形态，自部署≤4折，官转≤5.5折；形态不明待确认')
