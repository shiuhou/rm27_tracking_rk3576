#include "dart/green_detector.hpp"
#include "dart/async_frame.hpp"
#include "dart/nv21_pipeline.hpp"
#include "dart/target_json.hpp"
#include <cmath>
#include <iostream>
#include <stdexcept>
#include <string>

namespace {
int failures=0;
void check(bool value,const char*message){if(!value){++failures;std::cerr<<"FAIL: "<<message<<'\n';}}
maix::image::Image image(int shift=0){
    maix::image::Image frame(128,96);
    frame.fill_rect(0,0,128,96,20,20,20);
    for(int y=0;y<96;++y)for(int x=0;x<128;++x){
        const double r=std::hypot(x-(64+shift),y-48);
        if(r<=6)frame.set_pixel(x,y,10,235,10);
        if(r<=1)frame.set_pixel(x,y,245,245,245);
    }
    return frame;
}
void provenance(dart::TargetEstimate&target,uint64_t sequence){
    target.source_metadata_valid=true;target.source_sequence=sequence;
    target.source_received_us=target.timestamp_us;target.source_pts_raw=sequence*10;
    dart::stamp_pixel_measurement_source(target,sequence,sequence*10,target.timestamp_us,
        "unit_test_image_sequence","unit_test_input_time",false);
}
}

int main(){
    {
        dart::FrameMeasurementSchedule every_frame(180,180),lower_rate(90,180);
        dart::TimestampSchedule original90(90),independent180(180);
        dart::SequenceStats statistics;
        uint64_t now=1000000;unsigned frame_calls=0,old_full_rate_calls=0;
        for(uint64_t i=0;i<60;++i){
            if(i)now+=(i%3==0?6000:5500);
            const dart::FrameMetadata frame{i,i*10,now};statistics.observe(frame);
            frame_calls+=every_frame.due(frame);
            old_full_rate_calls+=independent180.due(now);
            check(lower_rate.due(frame)==original90.due(now),"90Hz path preserves the original deadline decisions");
        }
        check(frame_calls==60,"explicit 180-of-180 measures every actual new jittered frame");
        check(old_full_rate_calls<frame_calls,"independent equal-rate deadlines skip some jittered frames");
        const dart::FrameMetadata duplicate{59,99999,now+5000};statistics.observe(duplicate);
        const dart::FrameMetadata reversed{58,99998,now+6000};statistics.observe(reversed);
        check(!every_frame.due(duplicate)&&!every_frame.due(reversed),"new receive timestamps cannot refresh duplicate or reversed sequences");
        check(statistics.duplicate==1&&statistics.reversed==1,"source anomaly statistics remain available");
        check(every_frame.due({60,100000,now+5500}),"rejected old sources do not advance the accepted frame clock");
        bool repeated_time_rejected=false;
        try{every_frame.due({61,100010,now+5500});}catch(const std::invalid_argument&){repeated_time_rejected=true;}
        check(repeated_time_rejected,"new sequence with the same receive timestamp is rejected");
        check(every_frame.due({61,100010,now+11000}),"rejected equal timestamp does not consume the next sequence");
        dart::SequenceStats recovered;
        for(auto n:{10,9,11})recovered.observe({static_cast<uint64_t>(n),static_cast<uint64_t>(n),0});
        check(recovered.missing==0&&recovered.reversed==1&&recovered.highest_sequence==11,"reverse then recovery does not fabricate missing frames");
        dart::SequenceStats gap_recovery;
        for(auto n:{10,12,11,13})gap_recovery.observe({static_cast<uint64_t>(n),static_cast<uint64_t>(n),0});
        check(gap_recovery.missing==1&&gap_recovery.reversed==1&&gap_recovery.highest_sequence==13,"actual gap is counted once across reverse recovery");
    }
    dart::DetectorConfig config;
    config.enable_legacy_lab_candidates=false;
    config.enable_normalized_multiscale=true;
    config.enable_sparse_component_search=true;
    config.enable_capture_cone=false;
    dart::GreenLightDetector detector(config);
    dart::TargetEstimate source;
    bool confirmed=false,unconfirmed=false,separated=false;
    uint64_t time=1000000,sequence=0;
    for(int i=0;i<12;++i){
        auto frame=image(i>=6?6:0);time+=12000;++sequence;
        auto result=detector.process(frame,time,nullptr);
        if(result.green.state==dart::TrackState::Candidate){
            unconfirmed=true;check(!result.pixel_measurement.valid,"tentative candidate does not become raw measurement");
        }
        if(result.green.valid&&!result.green.predicted){
            confirmed=true;
            const auto&p=result.pixel_measurement;
            check(p.valid&&p.new_measurement&&p.tracker_confirmed&&p.center.valid,"confirmed current detection exports raw point");
            check(p.timestamp_us==time&&p.selected_count==1,"raw timestamp and unique selection");
            check(!p.position_covariance_valid&&!p.exposure_timestamp_valid,"no fabricated covariance or exposure timestamp");
            int count=0;
            for(const auto&candidate:detector.last_candidates())if(candidate.selected){
                ++count;check(p.center.x==candidate.center_x&&p.center.y==candidate.center_y,"raw center exactly equals selected pre-tracker candidate");
                check(p.bbox_x==candidate.bbox_x&&p.bbox_y==candidate.bbox_y&&p.bbox_w==candidate.bbox_w&&p.bbox_h==candidate.bbox_h,"raw bbox from selected green candidate");
            }
            check(count==1,"one selected candidate");
            separated|=std::hypot(p.center.x-result.green.center_x,p.center.y-result.green.center_y)>.1;
            provenance(result,sequence);source=result;
        }
    }
    check(confirmed&&unconfirmed,"real RGB detector confirms only after tentative observations");
    check(separated,"moving current-image point differs from temporal posterior");
    check(source.pixel_measurement.source_metadata_valid&&!source.pixel_measurement.source_pts_valid,"metadata source distinguishes a known sequence from unavailable PTS");
    dart::invalidate_uncalibrated(source);
    check(source.pixel_measurement.valid&&!source.safe_for_control&&!source.angles_valid&&!source.pose.valid,"pixel measurement does not bypass uncalibrated control flags");
    auto output=dart::predict_full180_output(source,detector.tracker_snapshot(),dart::ApplicationConfig{},time+5000);
    check(!output.pixel_measurement.valid&&!output.pixel_measurement.new_measurement,"pure predicted output does not copy raw measurement");
    dart::PixelMeasurementDelivery delivery;
    check(delivery.transfer(output,source,time+5000,true),"new snapshot forwards actual source measurement");
    check(output.pixel_measurement.timestamp_us==time&&output.pixel_measurement.measurement_age_us==5000,"output retains source timestamp and reports transport age");
    check(!output.safe_for_control&&!output.angles_valid,"delivery cannot promote hardware control flags");
    check(!delivery.transfer(output,source,time+6000,false)&&!output.pixel_measurement.valid,"no new snapshot produces no raw measurement");
    check(!delivery.transfer(output,source,time+7000,true)&&!output.pixel_measurement.valid,"same sequence cannot be redelivered even with an erroneous new-snapshot signal");
    auto duplicate=source;duplicate.pixel_measurement.source_sequence=source.source_sequence+1;duplicate.source_sequence++;
    check(!delivery.transfer(output,duplicate,time+8000,true),"same input timestamp cannot gain freshness from a new sequence");
    duplicate=source;duplicate.pixel_measurement.timestamp_us++;duplicate.source_received_us++;
    check(!delivery.transfer(output,duplicate,time+8000,true),"same sequence cannot gain freshness from a new timestamp");
    duplicate=source;duplicate.green.predicted=true;
    dart::PixelMeasurementDelivery fresh;
    check(!fresh.transfer(output,duplicate,time+8000,true),"a predicted source does not become a raw measurement");
    duplicate=source;duplicate.pixel_measurement.tracker_confirmed=false;
    check(!fresh.transfer(output,duplicate,time+8000,true),"unconfirmed source does not become raw measurement");
    duplicate=source;duplicate.pixel_measurement.selected_count=2;
    check(!fresh.transfer(output,duplicate,time+8000,true),"ambiguous selected candidates are rejected");
    duplicate=source;duplicate.classical_detection_ran=false;
    check(!fresh.transfer(output,duplicate,time+8000,true),"skipped detection cannot refresh a candidate");
    check(!fresh.transfer(output,source,time-1,true),"future measurement cannot be delivered before its source time");
    dart::PixelMeasurementDelivery delayed;
    check(delayed.transfer(output,source,time+1000000,true)&&output.pixel_measurement.measurement_age_us==1000000,"delivery preserves an old confirmed source and its age; freshness is a consumer gate");
    bool rejected=false;
    try{dart::stamp_pixel_measurement_source(source,1,2,time+1,"sequence","time",false);}catch(const std::invalid_argument&){rejected=true;}
    check(rejected,"source identity from a different input timestamp is rejected");
    const auto unknown_json=dart::target_estimate_json(source);
    check(unknown_json.find("\"position_covariance_px2\":null")!=std::string::npos,"unknown covariance serializes as null");
    rejected=false;
    try{dart::set_pixel_measurement_covariance(source,{1,2,2,1},"invalid covariance");}catch(const std::invalid_argument&){rejected=true;}
    check(rejected,"non-PSD external covariance rejected");
    rejected=false;
    try{dart::set_pixel_measurement_covariance(source,{0,.0005F,.0005F,0},"small indefinite covariance");}catch(const std::invalid_argument&){rejected=true;}
    check(rejected&&!source.pixel_measurement.position_covariance_valid,"zero-diagonal off-axis covariance is rejected at its own scale");
    rejected=false;
    try{dart::set_pixel_measurement_covariance(source,{1e-20F,2e-20F,2e-20F,1e-20F},"near-zero indefinite covariance");}catch(const std::invalid_argument&){rejected=true;}
    check(rejected,"near-zero indefinite covariance is rejected without an absolute tolerance floor");
    dart::set_pixel_measurement_covariance(source,{1e-20F,0,0,2e-20F},"near-zero diagonal PSD");
    check(source.pixel_measurement.position_covariance_valid,"near-zero diagonal PSD is accepted");
    dart::set_pixel_measurement_covariance(source,{1e-20F,1e-20F,1e-20F,1e-20F},"near-zero rank-one PSD");
    const auto retained_covariance=source.pixel_measurement.position_covariance_px2;
    const auto retained_model=source.pixel_measurement.covariance_source;
    rejected=false;
    try{dart::set_pixel_measurement_covariance(source,{1e-20F,1e-20F,0,1e-20F},"asymmetric covariance");}catch(const std::invalid_argument&){rejected=true;}
    check(rejected&&source.pixel_measurement.position_covariance_px2==retained_covariance&&source.pixel_measurement.covariance_source==retained_model,"rejected covariance cannot modify an existing valid model");
    dart::set_pixel_measurement_covariance(source,{4,.2F,.2F,9},"external model \"example\"\nnot a device calibration claim");
    check(source.pixel_measurement.position_covariance_valid,"explicit external PSD covariance can be represented");
    std::cout<<"JSON:"<<dart::target_estimate_json(source)<<'\n';
    if(failures){std::cerr<<failures<<" pixel measurement failures\n";return 1;}
    std::cout<<"Pixel measurement contract tests passed\n";return 0;
}
