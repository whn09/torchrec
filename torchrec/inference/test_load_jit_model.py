import torch

# 重要：在加载模型之前导入这些模块来注册自定义类
import fbgemm_gpu.split_table_batched_embeddings_ops_inference # 注册 FBGEMM 相关的自定义类

# 加载模型
loaded_model = torch.jit.load("/tmp/model.pt", map_location=torch.device('cuda:0'))

# 设置为评估模式
loaded_model.eval()

print(loaded_model)

# # 使用模型进行推理
# with torch.no_grad():
#     # 准备输入数据
#     input_data = torch.randn(1, 3, 224, 224)  # 示例输入
    
#     # 推理
#     output = loaded_model(input_data)
#     print(output)
