export const modelServices:Record<string,{label:string,protocol:string,url:string,models:string[]}>= {
 codex:{label:'Codex（ChatGPT 登录）',protocol:'codex',url:'',models:[]},
 deepseek:{label:'DeepSeek',protocol:'compatible',url:'https://api.deepseek.com',models:['deepseek-flash','deepseek-v4-pro']},
 glm:{label:'智谱 GLM',protocol:'compatible',url:'https://open.bigmodel.cn/api/paas/v4',models:['glm-5','glm-4.7']},
 kimi:{label:'Kimi / Moonshot',protocol:'compatible',url:'https://api.moonshot.cn/v1',models:['kimi-k3','kimi-k2.6','kimi-k2.7-code']},
 openai:{label:'OpenAI API',protocol:'openai',url:'https://api.openai.com/v1',models:[]},
 anthropic:{label:'Claude',protocol:'anthropic',url:'https://api.anthropic.com/v1',models:[]},
 google:{label:'Gemini',protocol:'google',url:'https://generativelanguage.googleapis.com/v1beta',models:[]},
 compatible:{label:'其他 OpenAI 兼容服务',protocol:'compatible',url:'',models:[]}
};
export function modelService(config:Record<string,any>):string {
 if(config.provider!=='compatible')return config.provider;
 let host='';try{host=new URL(config.base_url||'').hostname}catch{}
 return ({'api.deepseek.com':'deepseek','open.bigmodel.cn':'glm','api.moonshot.cn':'kimi','api.moonshot.ai':'kimi'} as Record<string,string>)[host]||'compatible';
}
