from paddlelite.lite import *

opt=Opt()
opt.set_model_file(r"output_inference/yolov3_mobilenet_v3_arm/model.pdmodel")
opt.set_param_file(r"output_inference/yolov3_mobilenet_v3_arm/model.pdiparams")
opt.set_optimize_out(r"output_inference/yolov3_mobilenet_v3_arm/model")
opt.set_valid_places(r"arm")
opt.set_model_type(r"naive_buffer")
opt.print_supported_ops()
opt.run()