cmake_minimum_required(VERSION 3.13)
project(dart_green_video_replay LANGUAGES CXX)

find_package(OpenCV 4 REQUIRED COMPONENTS core imgproc videoio)

add_library(dart_rgb_host STATIC
    maix_image_opencv.cpp
    ../../main/src/config.cpp
    ../../main/src/green_detector.cpp
    ../../main/src/green_detector_core.cpp
    ../../main/src/target_fusion.cpp
    ../../main/src/target_json.cpp
    ../../main/src/visual_motion.cpp
)

target_compile_features(dart_rgb_host PUBLIC cxx_std_17)
target_include_directories(dart_rgb_host PUBLIC
    ${CMAKE_CURRENT_SOURCE_DIR}
    ../../main/include
    ${OpenCV_INCLUDE_DIRS}
)
target_link_libraries(dart_rgb_host PUBLIC ${OpenCV_LIBS})

add_executable(dart_video_replay video_replay.cpp)
target_link_libraries(dart_video_replay PRIVATE dart_rgb_host)
add_executable(dart_rgb_stream rgb_stream.cpp)
target_link_libraries(dart_rgb_stream PRIVATE dart_rgb_host)

if(MSVC)
    target_compile_options(dart_rgb_host PRIVATE /W4)
    target_compile_options(dart_video_replay PRIVATE /W4)
    target_compile_options(dart_rgb_stream PRIVATE /W4)
else()
    target_compile_options(dart_rgb_host PRIVATE -Wall -Wextra -Wpedantic)
    target_compile_options(dart_video_replay PRIVATE -Wall -Wextra -Wpedantic)
    target_compile_options(dart_rgb_stream PRIVATE -Wall -Wextra -Wpedantic)
endif()
